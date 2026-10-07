"""Local snapshots and independent KIS-master/Yahoo/KRX collection."""
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from io import BytesIO
import hashlib,json,os,time,zipfile
import requests
from .config import RMMTError,local_path,Config
from .strategy import FLAGS,SEOUL,decimal,timestamp

MASTER_URL='https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip'
WIDTHS=[2,1,4,4,4,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,9,5,5,1,1,1,2,1,1,1,2,2,2,3,1,3,12,12,8,15,21,2,7,1,1,1,1,9,9,9,5,9,8,9,3,1,1,1]

def write_json(path,data):
    path=local_path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    os.replace(temp,path)

def load_json(path):
    path=local_path(path)
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):raise RMMTError(f'JSON 파일 읽기 실패: {path.name}') from None

KOSPI_WIDTHS=[2,1,4,4,4,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,9,5,5,1,1,1,2,1,1,1,2,2,2,3,1,3,12,12,8,15,21,2,7,1,1,1,1,1,9,9,9,5,9,8,9,3,1,1,1]

def parse_master(content,market='KOSDAQ'):
    if market not in ('KOSDAQ','KOSPI'):raise RMMTError('KIS master 시장 오류')
    widths=WIDTHS if market=='KOSDAQ' else KOSPI_WIDTHS
    managed,warning,halt,liquid,listing,preferred=(31,32,29,30,44,49) if market=='KOSDAQ' else (36,37,34,35,49,54)
    rows={};length=sum(widths)
    for line in content.decode('cp949').splitlines():
        if len(line)<21+length:raise RMMTError('KIS master 길이 변경')
        head,tail=line[:-length],line[-length:]
        symbol=head[:9].strip();fields=[];offset=0
        for width in widths:
            fields.append(tail[offset:offset+width].strip());offset+=width
        if symbol in rows:raise RMMTError('KIS master 코드 중복')
        if (fields[managed] not in ('Y','N') or fields[warning] not in ('00','01','02','03')
            or (market=='KOSDAQ' and fields[20] not in ('Y','N'))):
            raise RMMTError('KIS master 경고 코드 변경/누락')
        rows[symbol]={'symbol':symbol,'market':market,'name':head[21:].strip(),'group':fields[0],
          'common':fields[0] in ('ST','FS') and fields[preferred]=='0',
          'listing_date':fields[listing],'halted':fields[halt]=='Y','liquidating':fields[liquid]=='Y',
          'flags':{'MANAGED':fields[managed]=='Y','CAUTIONARY':market=='KOSDAQ' and fields[20]=='Y',
             'CAUTION':fields[warning]=='01','WARNING':fields[warning]=='02','RISK':fields[warning]=='03'}}
    if not rows:raise RMMTError('KIS master 비어 있음')
    return rows

class MasterProvider:
    def __init__(self,session=None,config=None):self.session=session or requests.Session();self.cached=None;self.at=None;self.config=config or Config()
    def get(self,max_age=None):
        max_age=self.config.master_cache_seconds if max_age is None else max_age
        now=datetime.now(timezone.utc)
        if self.cached is not None and (now-self.at).total_seconds()<=max_age:return self.cached,self.at
        rows={};sources=[];raw_paths=[];digests=[]
        # Both masters also preserve quote/ownership checks after a MARKET change.
        for market in ('KOSDAQ','KOSPI'):
            filename=market.lower()+'_code.mst'
            url='https://new.real.download.dws.co.kr/common/master/'+filename+'.zip'
            try:
                response=self.session.get(url,timeout=(self.config.connect_timeout,self.config.public_timeout));response.raise_for_status()
                with zipfile.ZipFile(BytesIO(response.content)) as archive:content=archive.read(filename)
                incoming=parse_master(content,market)
            except (requests.RequestException,ValueError,UnicodeError,zipfile.BadZipFile,KeyError):
                raise RMMTError('KIS '+market+' master 수집 실패') from None
            if set(rows)&set(incoming):raise RMMTError('두 시장 KIS 명부 종목코드 중복; 통합 집단 계산 차단')
            rows.update(incoming);sources.append(url)
            digest=hashlib.sha256(content).hexdigest();digests.append(digest)
            path=local_path(f'{self.config.data_dir}/raw/master/{market}_{now.strftime("%Y%m%dT%H%M%S%f")}_{digest[:12]}.mst')
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
            raw_paths.append(str(path.relative_to(local_path('.'))))
        self.cached={'rows':rows,'hash':hashlib.sha256(''.join(digests).encode()).hexdigest(),
            'source':' + '.join(sources),'retrieved_at':now.isoformat(),'raw_paths':raw_paths}
        self.at=now;return self.cached,now

def status_from_master(row,master,at):
    day=at.astimezone(SEOUL).date()
    return {'known':row is not None,**(row['flags'] if row else {flag:False for flag in FLAGS}),
        'available_at':master['retrieved_at'],'retrieved_at':master['retrieved_at'],
        'effective_at':datetime.combine(day,datetime.min.time(),SEOUL).isoformat(),
        'valid_until':datetime.combine(day+timedelta(days=1),datetime.min.time(),SEOUL).isoformat(),
        'source':master['source'],'snapshot_id':master['hash']}

class LiveQuotes:
    def __init__(self,broker,config,master=None):self.broker=broker;self.config=config;self.master=master or MasterProvider(config=config);self.cache={}
    def refresh_display(self,symbols):
        # Quote-only display polling; never change warning freshness or ledger state.
        tick=time.monotonic()
        if tick-getattr(self,'display_polled_at',float('-inf'))<self.config.dashboard_quote_refresh_seconds:return []
        self.display_polled_at=tick;errors=[]
        for symbol in sorted(set(symbols)):
            try:
                raw=self.broker.quote(symbol);price=decimal(raw['stck_prpr'])
                if price<=0:raise RMMTError('현재가 0 또는 결측')
                cached=dict(self.cache.get(symbol,{}))
                cached.update(price=str(price),observed_at=datetime.now(timezone.utc).isoformat(),source='KIS REST')
                self.cache[symbol]=cached
            except (RMMTError,KeyError,ValueError,TypeError) as exc:
                from .logs import diagnostic_error
                diagnostic_error('dashboard_current_price',exc,symbol=symbol,
                    cached_price=self.cache.get(symbol,{}).get('price'),
                    cached_observed_at=self.cache.get(symbol,{}).get('observed_at'))
                errors.append(symbol)
        return errors

    def get(self,symbol):
        master,_=self.master.get(min(self.config.master_cache_seconds,self.config.status_max_age));row=master['rows'].get(symbol)
        raw=self.broker.quote(symbol);at=datetime.now(timezone.utc)
        from .market import regulatory_from_kis
        status=regulatory_from_kis(raw,row,master,at)
        price=decimal(raw['stck_prpr']);upper=decimal(raw['stck_mxpr'])
        halted=raw.get('temp_stop_yn')!='N' or raw.get('sltr_yn')!='N' or not row or row['halted'] or row['liquidating']
        result={'name':row.get('name','') if row else '', 'price':str(price),'upper_limit':str(upper),'observed_at':at.isoformat(),
                'tradable':not halted and price>0,'traded_today':int(raw.get('acml_vol','0'))>0,
                'regulatory':status,'source':'KIS','tick':int(raw.get('aspr_unit','0'))}
        self.cache[symbol]=result
        return result

def collect_calendar(broker,start,end,progress=None):
    """Official KIS day calendar, one date per query; never infer holidays from weekdays."""
    sessions=[];raw=[];current=date.fromisoformat(start);last=date.fromisoformat(end)
    if (last-current).days>broker.config.calendar_max_days:raise RMMTError('달력 수집 최대 800일')
    while current<=last:
        compact=current.strftime('%Y%m%d')
        body,_=broker.request('GET','/uapi/domestic-stock/v1/quotations/chk-holiday','CTCA0903R',
            {'BASS_DT':compact,'CTX_AREA_NK':'','CTX_AREA_FK':''})
        matches=[r for r in body.get('output',[]) if r.get('bass_dt')==compact]
        if len(matches)!=1 or matches[0].get('opnd_yn') not in ('Y','N'):
            raise RMMTError('공식 달력 응답에서 요청일 개장 여부 확인 실패')
        raw.append(matches[0])
        if matches[0]['opnd_yn']=='Y':sessions.append(current.isoformat())
        current+=timedelta(days=1)
        completed=(current-date.fromisoformat(start)).days
        if progress and (completed%broker.config.progress_interval==0 or current>last):
            progress({'stage':'PREPARING_CALENDAR','completed':completed,'total':(last-date.fromisoformat(start)).days+1})
    return {'schema_version':1,'source':'KIS chk-holiday','calendar_complete':True,'start':start,'end':end,
            'retrieved_at':datetime.now(timezone.utc).isoformat(),'sessions':sessions,'raw':raw}

def yahoo_history(symbol,start,day,session=None,config=None,market='KOSDAQ'):
    config=config or Config()
    session=session or requests.Session()
    begin=int(datetime.combine(date.fromisoformat(start),datetime.min.time(),SEOUL).timestamp())
    end=int(datetime.combine(date.fromisoformat(day)+timedelta(days=1),datetime.min.time(),SEOUL).timestamp())
    try:
        response=session.get('https://query1.finance.yahoo.com/v8/finance/chart/'+symbol+('.KS' if market=='KOSPI' else '.KQ'),
            params={'period1':begin,'period2':end,'interval':'1d','events':'splits,div'},headers={'User-Agent':'Mozilla/5.0'},timeout=(config.connect_timeout,config.public_timeout))
        response.raise_for_status();result=response.json()['chart']['result'][0]
    except (requests.RequestException,ValueError,KeyError,TypeError,IndexError):return {},None
    content=response.content;digest=hashlib.sha256(content).hexdigest()
    raw_path=local_path(f'{config.data_dir}/raw/yahoo/{symbol}_{day}_{digest[:12]}.json')
    raw_path.parent.mkdir(parents=True,exist_ok=True);raw_path.write_bytes(content)
    closes=result.get('indicators',{}).get('quote',[{}])[0].get('close',[])
    timestamps=result.get('timestamp',[])
    if len(closes)!=len(timestamps):return {},None
    rows={datetime.fromtimestamp(t,SEOUL).date().isoformat():value for t,value in zip(timestamps,closes) if value is not None}
    return rows,{'source':'YAHOO','hash':digest,'path':str(raw_path.relative_to(local_path('.'))),
                 'split_events':result.get('events',{}).get('splits',{}),'symbol':symbol}

def verify_split_only(yahoo,nominal,split_events):
    """Validate quote.close vs unadjusted KRX close using only published split events."""
    if not yahoo:return False
    events=[]
    for event in split_events.values():
        at=datetime.fromtimestamp(event['date'],SEOUL).date().isoformat()
        events.append((at,decimal(event['numerator'])/decimal(event['denominator'])))
    checked=0
    for day,bar in nominal.items():
        if day not in yahoo or bar['volume']<=0 or bar['nominal_close']<=0:continue
        factor=decimal(1)
        for effective,ratio in events:
            if day<effective:factor*=ratio
        expected=decimal(bar['nominal_close'])/factor
        if abs(decimal(yahoo[day])/expected-1)>decimal('.01'):return False
        checked+=1
    return checked>=22

def collect_day(day,calendar,config,positions=None,master_provider=None,market_data=None,progress=None):
    """Must run on the requested session after close; never use today's type for old dates."""
    now=datetime.now(SEOUL)
    from datetime import time as day_time
    if now.date().isoformat()!=day or now.time()<day_time.fromisoformat(config.data_ready_time):
        raise RMMTError('당일 16시 이후에만 현재 master 기반 데이터 수집 가능. 과거 날짜는 보관된 스냅샷을 사용하세요.')
    sessions=calendar['sessions']
    if not calendar.get('calendar_complete') or not calendar.get('source') or day not in sessions:
        raise RMMTError('수집일을 포함하는 공식 완전 달력 필요')
    idx=sessions.index(day)
    if idx<config.formation_days or idx+1>=len(sessions):raise RMMTError('설정 포메이션/다음 거래일 달력 필요')
    if market_data is None:
        raise RMMTError('KIS 시세 공급자를 명시해야 합니다.')
    master,_=(master_provider or MasterProvider(config=config)).get()
    # Complete owned-position monitoring even if the new full-universe signal later fails.
    held_bars={}
    for symbol in (positions or {}):
        row=master['rows'].get(symbol)
        if not row:
            held_bars[symbol]={};continue
        listing=row['listing_date']
        if len(listing)!=8 or not listing.isdigit():
            held_bars[symbol]={};continue
        start=listing[:4]+'-'+listing[4:6]+'-'+listing[6:]
        held_bars[symbol]=market_data.history(symbol,start,day)
    daily={'schema_version':1,'date':day,'sessions':sessions[:idx+1],'bars':held_bars,
           'source':'KIS','retrieved_at':datetime.now(timezone.utc).isoformat(),'quality':'OFFICIAL_CURRENT'}
    write_json(config.daily_path,daily)
    caps=market_data.caps(day,master)
    symbols=set(caps)
    missing=symbols-set(master['rows'])
    if missing:raise RMMTError('KIS 명부와 공식 유형 master 불일치: '+','.join(sorted(missing)[:10]))
    common=[s for s in symbols if master['rows'][s]['common']]
    master_common={s for s,r in master['rows'].items() if r['common'] and (config.market=='COMBINED' or r.get('market','KOSDAQ')==config.market)}
    if master_common-set(common):raise RMMTError('전체 보통주 시총 누락; D1 계산 금지')
    common.sort(key=lambda s:(caps[s]['market_cap'],s))
    from .strategy import decile_bounds
    start_rank,stop_rank=decile_bounds(len(common),config.decile)
    needed=set(common[start_rank:stop_rank])|set(positions or {})
    securities=[];lineage=[];daily_positions=dict(held_bars)
    for completed,symbol in enumerate(common,1):
        m=master['rows'][symbol];cap=caps[symbol]['market_cap']
        if cap<=0:raise RMMTError('KIS 보통주 시총 결측/0')
        row={'symbol':symbol,'name':m['name'],'market':m.get('market','KOSDAQ'),'security_type':'COMMON','market_cap':str(cap),
             'market_cap_date':day,'classification_source':'KIS master: ST/FS and preferred=0',
             'signal_source':'YAHOO','adjustment_basis':'UNKNOWN','shock_history_complete':False,
             'tradable_on_signal':not m['halted'] and not m['liquidating'],'regulatory':caps[symbol]['regulatory'],
             'daily':[{'date':day,'nominal_close':caps[symbol]['nominal_close'],'volume':caps[symbol]['volume'],'split_adjusted_close':None}]}
        if symbol in needed:
            listed=m['listing_date']
            if len(listed)!=8 or not listed.isdigit():raise RMMTError('공식 상장일 누락')
            start=listed[:4]+'-'+listed[4:6]+'-'+listed[6:]
            bars=held_bars[symbol] if symbol in held_bars else market_data.history(symbol,start,day)
            history,provenance=yahoo_history(symbol,start,day,config=config,market=m.get('market','KOSDAQ'))
            if provenance:lineage.append(provenance)
            verified=provenance is not None and verify_split_only(history,bars,provenance['split_events'])
            row['daily']=[bar|{'split_adjusted_close':history.get(d)} for d,bar in sorted(bars.items()) if d<=day]
            row['adjustment_basis']='SPLIT_ONLY' if verified else 'UNKNOWN'
            # Require nominal history reaching first trading day since listing, not a truncated 22-row buffer.
            row['shock_history_complete']=bool(bars) and min(bars)<=start and verified
            if symbol in (positions or {}):daily_positions[symbol]=bars
        securities.append(row)
        if progress and (completed%config.progress_interval==0 or completed==len(common)):
            progress({'stage':'PREPARING_SIGNAL_HISTORY','completed':completed,'total':len(common)})
    # Held securities absent from today's master cannot be silently inferred delisted.
    for symbol in set(positions or {})-set(daily_positions):daily_positions[symbol]={}
    finished=datetime.now(SEOUL)
    if finished.date().isoformat()!=day or finished.time()<day_time.fromisoformat(config.data_ready_time):
        raise RMMTError('수집 도중 날짜 변경: 현재 master/시총을 과거 신호로 확정하지 않습니다.')
    cutoff=finished.astimezone(timezone.utc).isoformat()
    snapshot={'schema_version':1,'market':config.market,'signal_date':day,'execution_date':sessions[idx+1],
        'sessions':sessions,'calendar_complete':True,'universe_complete':True,'data_cutoff_at':cutoff,
        'source':'KIS REST + KIS master + Yahoo','quality':'OFFICIAL_CURRENT','securities':securities,
        'lineage':{'master':{k:v for k,v in master.items() if k!='rows'},'yahoo':lineage,'kis':market_data.lineage,'calendar':calendar['source']}}
    daily={'schema_version':1,'date':day,'sessions':sessions[:idx+1],'bars':daily_positions,'source':'KIS',
           'retrieved_at':cutoff,'quality':'OFFICIAL_CURRENT'}
    write_json(config.daily_path,daily)
    write_json(f'{config.data_dir}/snapshots/{day}.json',snapshot)
    weekly=date.fromisoformat(day).isocalendar()[:2]!=date.fromisoformat(sessions[idx+1]).isocalendar()[:2]
    # Collection archives prices; scheduled 10:00 selection is published separately.
    return {'date':day,'common_count':len(common),'d1_count':len(common)//10+bool(len(common)%10),
        'decile':config.decile,'decile_count':stop_rank-start_rank,'weekly_signal_ready':False}
