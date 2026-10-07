"""Independent KIS market-cap and original-price history collector."""
from datetime import datetime,date,timedelta,timezone
import hashlib,json
from .config import RMMTError,local_path,Config
from .strategy import decimal
from .data import write_json,load_json,status_from_master

class KisMarketData:
    def __init__(self,broker,progress=None,cache_dir=None):
        self.broker=broker;self.progress=progress or (lambda value:None);self.lineage=[];self.config=broker.config if isinstance(getattr(broker,'config',None),Config) else Config();self.cache_dir=local_path(cache_dir or self.config.data_dir+'/kis_history')
    def _archive(self,symbol,start,end,body):
        encoded=json.dumps(body,sort_keys=True).encode();digest=hashlib.sha256(encoded).hexdigest()
        path=f'{self.config.data_dir}/raw/kis/{symbol}_{start}_{end}_{digest[:12]}.json'
        write_json(path,{'source':'KIS','retrieved_at':datetime.now(timezone.utc).isoformat(),'response':body})
        self.lineage.append({'symbol':symbol,'start':start,'end':end,'hash':digest,'path':path})
    def _daily(self,symbol,start,end):
        body,_=self.broker.request('GET','/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice','FHKST03010100',
            {'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':symbol,'FID_INPUT_DATE_1':start.replace('-',''),
             'FID_INPUT_DATE_2':end.replace('-',''),'FID_PERIOD_DIV_CODE':'D','FID_ORG_ADJ_PRC':'1'})
        self._archive(symbol,start,end,body)
        rows={}
        for raw in body.get('output2',[]):
            compact=raw.get('stck_bsop_date','')
            if not compact:continue
            if len(compact)!=8 or not compact.isdigit():raise RMMTError('KIS 일봉 날짜 계약 변경')
            day=compact[:4]+'-'+compact[4:6]+'-'+compact[6:]
            if not start<=day<=end:raise RMMTError('KIS 일봉 조회 범위 밖 자료')
            if day in rows:raise RMMTError('KIS 일봉 날짜 중복')
            rows[day]={'date':day,'nominal_close':int(raw['stck_clpr']),'volume':int(raw['acml_vol'])}
        return rows
    def caps(self,day,master):
        rows={};common=[symbol for symbol,row in master['rows'].items() if row['common'] and (self.config.market=='COMBINED' or row.get('market','KOSDAQ')==self.config.market)]
        for index,symbol in enumerate(sorted(common),1):
            raw=self.broker.quote(symbol);price=decimal(raw['stck_prpr']);shares=int(raw['lstn_stcn'])
            if price<=0 or shares<=0:raise RMMTError('전체 보통주 현재 종가/상장주식수 누락: '+symbol)
            daily=self._daily(symbol,day,day)
            if day not in daily:raise RMMTError('KIS 당일 종가 확정 자료 없음: '+symbol)
            last=daily[day]
            if last['volume']>0 and decimal(last['nominal_close'])!=price:
                raise RMMTError('KIS 현재가와 당일 일봉 종가 불일치: '+symbol)
            at=datetime.now(timezone.utc)
            status=regulatory_from_kis(raw,master['rows'][symbol],master,at)
            self._archive(symbol,day,day,{'quote':raw})
            rows[symbol]={'market_cap':price*shares,'nominal_close':int(price),'volume':last['volume'],'regulatory':status}
            if index%self.config.progress_interval==0 or index==len(common):self.progress({'stage':'KIS_ALL_COMMON_CAP','completed':index,'total':len(common)})
        return rows
    def history(self,symbol,start,day):
        cache_path=self.cache_dir/(symbol+'.json');cached={};cursor=day
        if local_path(cache_path).exists():
            previous=load_json(cache_path)
            if previous.get('from_listing')==start and previous.get('complete_from_listing') is True:
                cached={d:r for d,r in previous['bars'].items() if d<=day}
        if cached:
            latest=max(cached);query_start=(date.fromisoformat(latest)-timedelta(days=self.config.history_overlap_days)).isoformat()
            # Refresh overlap explicitly; market API is requested as original prices.
            if query_start<start:query_start=start
            if (date.fromisoformat(day)-date.fromisoformat(latest)).days>self.config.history_cache_gap_days:
                cached={}
        if cached:
            batch=self._daily(symbol,query_start,day)
            # API returns at most 100 sessions. A gap must not be marked complete.
            if batch and min(batch)>latest:cached={}
            else:cached.update(batch)
        if not cached:
            result={};cursor=day
            for _ in range(self.config.history_max_pages):
                batch=self._daily(symbol,start,cursor)
                if not batch:break
                oldest=min(batch);result.update(batch)
                if oldest<=start:break
                next_cursor=(date.fromisoformat(oldest)-timedelta(days=1)).isoformat()
                if next_cursor>=cursor:raise RMMTError('KIS 일봉 과거 커서 역행 실패')
                cursor=next_cursor
            else:raise RMMTError('KIS 일봉 역사 500페이지 초과')
            cached=result
        complete=bool(cached) and min(cached)<=start
        write_json(cache_path,{'source':'KIS','from_listing':start,'asof_date':day,'complete_from_listing':complete,
                              'bars':cached,'retrieved_at':datetime.now(timezone.utc).isoformat()})
        self.progress({'stage':'KIS_HISTORY','symbol':symbol,'observations':len(cached),'complete':complete})
        return cached

def regulatory_from_kis(raw,row,master,at):
    status=status_from_master(row,master,at)
    warning=raw.get('mrkt_warn_cls_code');managed=raw.get('mang_issu_cls_code')
    if warning not in ('00','01','02','03') or managed not in ('0','1','N','Y','00','01'):
        status['known']=False
    else:
        current={'MANAGED':managed in ('1','Y','01'),'CAUTION':warning=='01','WARNING':warning=='02','RISK':warning=='03'}
        for flag,active in current.items():status[flag]=status[flag] or active
    status['source']='KIS inquire-price + KIS market master'
    status['snapshot_id']=hashlib.sha256((master['hash']+json.dumps(raw,sort_keys=True)).encode()).hexdigest()
    # Both components must be current. Master timestamp remains the conservative freshness bound.
    return status
