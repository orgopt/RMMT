"""KIS market-cap, original-price and adjusted-price history collector."""
from datetime import datetime,date,timedelta,timezone
import hashlib,json
from .config import RMMTError,local_path,Config
from .strategy import decimal
from .data import write_json,load_json,status_from_master

class KisMarketData:
    def __init__(self,broker,progress=None,cache_dir=None):
        self.broker=broker;self.progress=progress or (lambda value:None);self.lineage=[];self.config=broker.config if isinstance(getattr(broker,'config',None),Config) else Config();self.cache_dir=local_path(cache_dir or self.config.data_dir+'/kis_history')
    def _archive(self,symbol,start,end,body,price_mode='1'):
        encoded=json.dumps(body,sort_keys=True).encode();digest=hashlib.sha256(encoded).hexdigest()
        path=f'{self.config.data_dir}/raw/kis/{symbol}_{start}_{end}_mode{price_mode}_{digest[:12]}.json'
        write_json(path,{'source':'KIS','price_mode':price_mode,'retrieved_at':datetime.now(timezone.utc).isoformat(),'response':body})
        self.lineage.append({'symbol':symbol,'start':start,'end':end,'price_mode':price_mode,'hash':digest,'path':path})
    def _daily(self,symbol,start,end,price_mode='1'):
        self.progress({'stage':'KIS_ADJUSTED_HISTORY' if price_mode=='0' else 'KIS_HISTORY','symbol':symbol,'server':'KIS'})
        body,_=self.broker.request('GET','/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice','FHKST03010100',
            {'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':symbol,'FID_INPUT_DATE_1':start.replace('-',''),
             'FID_INPUT_DATE_2':end.replace('-',''),'FID_PERIOD_DIV_CODE':'D','FID_ORG_ADJ_PRC':price_mode})
        # A malformed successful HTTP response is a data error, never a fatal parser exception.
        rows={};field='adjusted_close' if price_mode=='0' else 'nominal_close'
        try:
            raw_rows=body['output2']
            if not isinstance(raw_rows,list):raise TypeError()
            for raw in raw_rows:
                if not isinstance(raw,dict):raise TypeError()
                compact=raw.get('stck_bsop_date','')
                if compact=='':continue  # Empty trailing API row.
                if not isinstance(compact,str) or len(compact)!=8 or not compact.isdigit():raise ValueError()
                day=date.fromisoformat(compact[:4]+'-'+compact[4:6]+'-'+compact[6:]).isoformat()
                if not start<=day<=end:raise RMMTError('KIS 일봉 조회 범위 밖 자료')
                if day in rows:raise RMMTError('KIS 일봉 날짜 중복')
                price=decimal(raw['stck_clpr']);volume=decimal(raw['acml_vol'])
                if price<0 or volume<0 or volume!=volume.to_integral_value():raise ValueError()
                if field=='nominal_close' and price!=price.to_integral_value():raise ValueError()
                rows[day]={'date':day,field:int(price) if field=='nominal_close' else str(price),'volume':int(volume)}
        except (AttributeError,KeyError,TypeError,ValueError,OverflowError):
            raise RMMTError(f'KIS {symbol} 일봉 응답 형식 오류 — 재조회 필요') from None
        self._archive(symbol,start,end,body,price_mode)
        self.progress({'stage':'KIS_ADJUSTED_HISTORY' if price_mode=='0' else 'KIS_HISTORY','symbol':symbol,'server':'KIS','observations':len(rows)})
        return rows
    def caps(self,day,master,bootstrap=False):
        """Bulk master cap in 100 million KRW; no per-symbol quote requests."""
        rows={};self.cap_exclusions={}
        common=sorted(s for s,r in master['rows'].items() if r['common'])
        for index,symbol in enumerate(common,1):
            row=master['rows'][symbol];value=row.get('master_market_cap_oku')
            if value is None or value<=0:
                if row.get('liquidating') is True:
                    self.cap_exclusions[symbol]={'reason':'LIQUIDATING_MISSING_CAP','name':row.get('name',''),'source':'KIS_MASTER'}
                    self.progress({'stage':'CAP_EXCLUDED','symbol':symbol,'name':row.get('name',''),
                                   'reason':'정리매매 종목 명부 시총 없음 — 수집·십분위 대상 제외'})
                else:raise RMMTError('KIS 명부 시가총액 누락: '+symbol)
            else:
                rows[symbol]={'market_cap':decimal(value)*100000000,'nominal_close':0,'volume':0,
                              'regulatory':status_from_master(row,master,datetime.now(timezone.utc))}
            self.progress({'stage':'KIS_ALL_COMMON_CAP','completed':index,'total':len(common),'server':'KIS'})
        return rows
    def history(self,symbol,start,day,required_dates=None):
        cache_path=self.cache_dir/(symbol+'.json');cached={};cursor=day;coverage=False
        required=[d for d in (required_dates or []) if start<=d<=day]
        if local_path(cache_path).exists():
            try:
                previous=load_json(cache_path)
                if previous.get('from_listing','9999')<=start and previous.get('asof_date','9999')<=day and previous.get('complete_from_listing') is True:
                    cached={d:r for d,r in previous['bars'].items() if start<=d<=day}
                    if any(not isinstance(r,dict) or decimal(r['nominal_close'])<0
                           or type(r['volume']) is not int or r['volume']<0 for r in cached.values()):raise ValueError()
                    coverage=True
            except (RMMTError,KeyError,TypeError,ValueError,AttributeError):cached={};coverage=False
        if cached:
            if previous.get('asof_date')==day and max(cached)==day and all(d in cached for d in required):
                self.progress({'stage':'KIS_HISTORY','symbol':symbol,'observations':len(cached),'complete':True,'cached':True})
                return cached
            latest=max(cached);query_start=(date.fromisoformat(latest)-timedelta(days=self.config.history_overlap_days)).isoformat()
            missing=[d for d in required if d not in cached]
            if missing:query_start=min(query_start,min(missing))
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
            result={};cursor=day;coverage=False
            for _ in range(self.config.history_max_pages):
                batch=self._daily(symbol,start,cursor)
                if not batch:
                    coverage=True;break
                oldest=min(batch);result.update(batch)
                if oldest<=start:
                    coverage=True;break
                next_cursor=(date.fromisoformat(oldest)-timedelta(days=1)).isoformat()
                if next_cursor>=cursor:raise RMMTError('KIS 일봉 과거 커서 역행 실패')
                cursor=next_cursor
            else:raise RMMTError('KIS 일봉 역사 500페이지 초과')
            cached=result
        cached={d:r for d,r in cached.items() if start<=d<=day}
        # Requested start may be a holiday; reaching that boundary is complete.
        complete=bool(cached) and coverage
        write_json(cache_path,{'source':'KIS','from_listing':start,'asof_date':day,'complete_from_listing':complete,
                              'bars':cached,'retrieved_at':datetime.now(timezone.utc).isoformat()})
        self.progress({'stage':'KIS_HISTORY','symbol':symbol,'observations':len(cached),'complete':complete})
        return cached

    def adjusted_history(self,symbol,start,day,required_dates):
        """Refresh the entire required window on a new asof; never mix adjustment anchors."""
        cache_path=local_path(self.config.data_dir+'/kis_adjusted_history')/(symbol+'.json')
        required=[d for d in required_dates if start<=d<=day]
        def complete(rows):
            return bool(required) and all(decimal(rows.get(d,{}).get('adjusted_close',0))>0 for d in required)
        if cache_path.exists():
            try:
                cached=load_json(cache_path)
                rows=cached['bars']
                if (cached.get('schema_version')==2 and cached.get('source')=='KIS'
                    and cached.get('price_mode')=='0' and cached.get('asof_date')==day
                    and cached.get('start','9999')<=start and isinstance(rows,dict) and complete(rows)
                    and isinstance(cached.get('lineage'),list) and cached['lineage']):
                    self.lineage.extend(cached['lineage'])
                    self.progress({'stage':'KIS_ADJUSTED_HISTORY','symbol':symbol,'cached':True,'observations':len(rows)})
                    return {d:bar for d,bar in rows.items() if start<=d<=day}
            except (RMMTError,KeyError,TypeError,ValueError,AttributeError):pass
        result={};cursor=day;lineage_start=len(self.lineage)
        for _ in range(self.config.history_max_pages):
            self.progress({'stage':'KIS_ADJUSTED_HISTORY','symbol':symbol,'server':'KIS','observations':len(result)})
            batch=self._daily(symbol,start,cursor,price_mode='0')
            if not batch:break
            result.update(batch);oldest=min(batch)
            if oldest<=start or all(d in result for d in required):break
            next_cursor=(date.fromisoformat(oldest)-timedelta(days=1)).isoformat()
            if next_cursor>=cursor:raise RMMTError('KIS 수정주가 과거 커서 역행 실패')
            cursor=next_cursor
        else:raise RMMTError('KIS 수정주가 최대 조회 페이지 초과')
        if result:
            write_json(cache_path,{'schema_version':2,'source':'KIS','price_mode':'0','start':start,'asof_date':day,
                'required_dates_complete':complete(result),'bars':result,'lineage':self.lineage[lineage_start:],
                'retrieved_at':datetime.now(timezone.utc).isoformat()})
        self.progress({'stage':'KIS_ADJUSTED_HISTORY','symbol':symbol,'complete':complete(result),'observations':len(result)})
        return result

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
