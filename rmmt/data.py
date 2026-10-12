"""KIS snapshots and collection; Yahoo helpers are retained for legacy offline diagnostics."""
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
    with temp.open('w',encoding='utf-8') as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2,default=str)
    os.replace(temp,path)

def load_json(path):
    path=local_path(path)
    try:
        with path.open(encoding='utf-8') as stream:return json.load(stream)
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
          'master_market_cap_oku':int(fields[-5]) if fields[-5].isdigit() else None,
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

def history_start(day,listing):
    """One calendar year ending at the completed price reference day."""
    end=date.fromisoformat(day)
    try:begin=end.replace(year=end.year-1)
    except ValueError:begin=end.replace(year=end.year-1,day=28)
    return max(begin.isoformat(),listing)

def yahoo_history(symbol,start,day,session=None,config=None,market='KOSDAQ',_full=False,progress=None,required_dates=None):
    config=config or Config()
    session=session or requests.Session()
    cache_path=local_path(f'{config.data_dir}/yahoo_history/{market}_{symbol}.json')
    def valid_price(value):
        try:return value is not None and decimal(value)>0
        except RMMTError:return False
    cached=None
    if not _full and cache_path.exists():
        try:
            candidate=load_json(cache_path)
            if (candidate.get('start','9999')<=start and candidate.get('asof_date','')<=day
                and isinstance(candidate.get('rows'),dict) and candidate['rows'] and isinstance(candidate.get('provenance'),dict)):
                cached=dict(candidate,rows={d:v for d,v in candidate['rows'].items() if start<=d<=day and valid_price(v)})
        except (RMMTError,ValueError,TypeError,KeyError):pass
    required=set(required_dates if required_dates is not None else [day])
    def complete(rows):
        return all(valid_price(rows.get(d)) for d in required)
    if cached and cached['asof_date']==day and complete(cached['rows']):
        if progress:progress('Yahoo')
        return cached['rows'],cached['provenance']
    query_start=start
    if cached:
        latest=max(cached['rows']) if cached['rows'] else start
        query_start=max(start,(date.fromisoformat(latest)-timedelta(days=config.history_overlap_days)).isoformat())
        missing=[d for d in required if not valid_price(cached['rows'].get(d))]
        if missing:query_start=min(query_start,max(start,min(missing)))
    begin=int(datetime.combine(date.fromisoformat(query_start),datetime.min.time(),SEOUL).timestamp())
    end=int(datetime.combine(date.fromisoformat(day)+timedelta(days=1),datetime.min.time(),SEOUL).timestamp())
    if progress:progress('Yahoo')
    try:
        response=session.get('https://query1.finance.yahoo.com/v8/finance/chart/'+symbol+('.KS' if market=='KOSPI' else '.KQ'),
            params={'period1':begin,'period2':end,'interval':'1d','events':'splits,div'},headers={'User-Agent':'Mozilla/5.0'},timeout=(config.connect_timeout,config.public_timeout))
        response.raise_for_status();result=response.json()['chart']['result'][0]
    except (requests.RequestException,ValueError,KeyError,TypeError,IndexError):return {},None
    content=response.content;digest=hashlib.sha256(content).hexdigest()
    raw_path=local_path(f'{config.data_dir}/raw/yahoo/{symbol}_{day}_{digest[:12]}.json')
    raw_path.parent.mkdir(parents=True,exist_ok=True);raw_path.write_bytes(content)
    # Legacy Yahoo helper: malformed provider payloads must remain retryable.
    try:
        closes=result['indicators']['quote'][0]['close']
        timestamps=result['timestamp']
        if not isinstance(closes,list) or not isinstance(timestamps,list) or len(closes)!=len(timestamps):return {},None
        if any(type(t) not in (int,float) for t in timestamps):return {},None
        rows={datetime.fromtimestamp(t,SEOUL).date().isoformat():value for t,value in zip(timestamps,closes) if valid_price(value)}
        splits=result.get('events',{}).get('splits',{})
        if not isinstance(splits,dict):return {},None
        for event in splits.values():
            if not isinstance(event,dict) or decimal(event['numerator'])<=0 or decimal(event['denominator'])<=0:return {},None
            datetime.fromtimestamp(event['date'],SEOUL)
    except (AttributeError,KeyError,TypeError,ValueError,IndexError,OverflowError,OSError,RMMTError):return {},None
    rows={d:v for d,v in rows.items() if start<=d<=day}
    if cached:
        old_splits=cached['provenance'].get('split_events',{})
        changed=any(k not in old_splits or old_splits[k]!=v for k,v in splits.items())
        changed=changed or any(k not in splits and begin<=int(v.get('date',k))<end
            for k,v in old_splits.items())
        changed=changed or any(abs(decimal(v)/decimal(cached['rows'][d])-1)>decimal('0.000001')
            for d,v in rows.items() if d in cached['rows'] and decimal(cached['rows'][d])!=0)
        if changed:return yahoo_history(symbol,start,day,session,config,market,_full=True,progress=progress,required_dates=required)
        if not rows or not set(rows).intersection(cached['rows']):
            return yahoo_history(symbol,start,day,session,config,market,_full=True,progress=progress,required_dates=required)
        rows=cached['rows']|rows;splits=old_splits|splits
    provenance={'source':'YAHOO','hash':digest,'path':str(raw_path.relative_to(local_path('.'))),
                 'split_events':splits,'symbol':symbol}
    if cached:
        provenance['cache_sources']=cached['provenance'].get('cache_sources',[])+[{'hash':cached['provenance']['hash'],'path':cached['provenance']['path']}]
    if rows:write_json(cache_path,{'start':start,'asof_date':day,'last_price_date':max(rows),
        'required_dates_complete':complete(rows),'rows':rows,'provenance':provenance})
    return rows,provenance

def verify_split_only(yahoo,nominal,split_events,required_dates=None):
    """Validate quote.close vs unadjusted KRX close using only published split events."""
    if not yahoo:return False
    events=[]
    for event in split_events.values():
        at=datetime.fromtimestamp(event['date'],SEOUL).date().isoformat()
        events.append((at,decimal(event['numerator'])/decimal(event['denominator'])))
    checked=0
    for day,bar in nominal.items():
        if required_dates is not None and day not in required_dates:continue
        if day not in yahoo or bar['volume']<=0 or bar['nominal_close']<=0:continue
        factor=decimal(1)
        for effective,ratio in events:
            if day<effective:factor*=ratio
        expected=decimal(bar['nominal_close'])/factor
        if abs(decimal(yahoo[day])/expected-1)>decimal('.01'):return False
        checked+=1
    return checked>=22

def collect_day(day,calendar,config,positions=None,master_provider=None,market_data=None,progress=None,bootstrap=False,deadline=None):
    """Must run on the requested session after close; never use today's type for old dates."""
    now=datetime.now(SEOUL)
    def check_deadline():
        if deadline is not None and datetime.now(SEOUL)>=deadline:
            raise RMMTError('자료 수집 중 주문창 종료: 신규 주문 없음')
    check_deadline()
    original_progress=progress
    def checked_progress(value):
        check_deadline()
        if original_progress:original_progress(value)
    progress=checked_progress
    from datetime import time as day_time
    if not bootstrap and (now.date().isoformat()!=day or now.time()<day_time.fromisoformat(config.data_ready_time)):
        raise RMMTError('당일 16시 이후에만 현재 master 기반 데이터 수집 가능. 과거 날짜는 보관된 스냅샷을 사용하세요.')
    if bootstrap and day>=now.date().isoformat():raise RMMTError('최초 자료는 완료된 과거 거래일까지 수집해야 합니다.')
    sessions=calendar['sessions']
    if not calendar.get('calendar_complete') or not calendar.get('source') or day not in sessions:
        raise RMMTError('수집일을 포함하는 공식 완전 달력 필요')
    idx=sessions.index(day)
    if idx<config.formation_days or idx+1>=len(sessions):raise RMMTError('설정 포메이션/다음 거래일 달력 필요')
    if market_data is None:
        raise RMMTError('KIS 시세 공급자를 명시해야 합니다.')
    master,_=(master_provider or MasterProvider(config=config)).get()
    if bootstrap:
        # Newly listed today cannot have a previous-session closing history.
        master=dict(master,rows={symbol:row for symbol,row in master['rows'].items()
            if row.get('listing_date','')<=day.replace('-','')})
    # Complete owned-position monitoring even if the new full-universe signal later fails.
    formation_dates=sessions[idx-config.formation_days:idx+1]
    held_bars={}
    for symbol in (positions or {}):
        row=master['rows'].get(symbol)
        if not row:
            held_bars[symbol]={};continue
        listing=row['listing_date']
        if len(listing)!=8 or not listing.isdigit():
            held_bars[symbol]={};continue
        start=max(formation_dates[0],listing[:4]+'-'+listing[4:6]+'-'+listing[6:])
        held_bars[symbol]=market_data.history(symbol,start,day,required_dates=formation_dates)
    check_deadline()
    caps=market_data.caps(day,master,bootstrap=True) if bootstrap else market_data.caps(day,master)
    symbols=set(caps)
    missing=symbols-set(master['rows'])
    if missing:raise RMMTError('KIS 명부와 공식 유형 master 불일치: '+','.join(sorted(missing)[:10]))
    common=[s for s in symbols if master['rows'][s]['common']]
    master_common={s for s,r in master['rows'].items() if r['common']}
    exclusions=getattr(market_data,'cap_exclusions',{})
    if not isinstance(exclusions,dict):exclusions={}
    if any(s not in master_common or not master['rows'][s].get('liquidating')
           or record.get('reason')!='LIQUIDATING_MISSING_CAP' for s,record in exclusions.items()):
        raise RMMTError('시총 수집 제외 계약 위반')
    if master_common-set(common)-set(exclusions):raise RMMTError('전체 보통주 시총 누락; D1 계산 금지')
    common.sort(key=lambda s:(caps[s]['market_cap'],s))
    from .strategy import decile_bounds,rank_universe
    tie_seed=__import__('secrets').token_hex(16)
    universe=rank_universe([{'symbol':s,'market':master['rows'][s].get('market','KOSDAQ'),
                            'market_cap':caps[s]['market_cap']} for s in common],config,tie_seed)
    start_rank,stop_rank=decile_bounds(len(universe),config.decile)
    needed={r['symbol'] for r in universe[start_rank:stop_rank]}|set(positions or {})
    history_total=len(set(common)&needed);history_completed=0
    securities=[];daily_positions=dict(held_bars)
    for symbol in common:
        m=master['rows'][symbol];cap=caps[symbol]['market_cap']
        if cap<=0:raise RMMTError('KIS 보통주 시총 결측/0')
        row={'symbol':symbol,'name':m['name'],'market':m.get('market','KOSDAQ'),'security_type':'COMMON','market_cap':str(cap),
             'market_cap_date':day,'market_cap_source':'KIS_MASTER','market_cap_basis':'PREVIOUS_DAY_MASTER_APPROX','market_cap_actual_date':None,'classification_source':'KIS master: ST/FS and preferred=0',
             'signal_source':'KIS','adjustment_basis':'UNKNOWN','shock_history_complete':False,'listing_date':m['listing_date'],
             'tradable_on_signal':not m['halted'] and not m['liquidating'],'regulatory':caps[symbol]['regulatory'],
             'daily':[]}
        if symbol in needed:
            listed=m['listing_date']
            if len(listed)!=8 or not listed.isdigit():raise RMMTError('공식 상장일 누락')
            start=max(formation_dates[0],listed[:4]+'-'+listed[4:6]+'-'+listed[6:])
            def history_progress(server):
                if progress:progress({'stage':'PREPARING_SIGNAL_HISTORY','completed':history_completed,
                                      'total':history_total,'server':server,'symbol':symbol})
            history_progress('KIS')
            bars=held_bars[symbol] if symbol in held_bars else market_data.history(symbol,start,day,required_dates=formation_dates)
            adjusted=market_data.adjusted_history(symbol,start,day,required_dates=formation_dates)
            check_deadline()
            # Keep missing original bars visible instead of inferring prices or volumes.
            row['daily']=[{'date':d,'nominal_close':bars.get(d,{}).get('nominal_close'),
                'volume':bars.get(d,{}).get('volume'),'adjusted_close':adjusted.get(d,{}).get('adjusted_close')}
                for d in formation_dates if d>=start]
            row['adjustment_basis']='KIS_ADJUSTED'
            row['shock_history_complete']=all(d in bars and decimal(adjusted.get(d,{}).get('adjusted_close',0))>0 for d in formation_dates)
            if symbol in (positions or {}):daily_positions[symbol]=bars
            history_completed+=1
            if progress:progress({'stage':'PREPARING_SIGNAL_HISTORY','completed':history_completed,'total':history_total,'server':'KIS','symbol':symbol})
        securities.append(row)
    # Held securities absent from today's master cannot be silently inferred delisted.
    for symbol in set(positions or {})-set(daily_positions):daily_positions[symbol]={}
    finished=datetime.now(SEOUL)
    check_deadline()
    if (not bootstrap and (finished.date().isoformat()!=day or finished.time()<day_time.fromisoformat(config.data_ready_time))) or (bootstrap and finished.date()!=now.date()):
        raise RMMTError('수집 도중 날짜 변경: 현재 master/시총을 과거 신호로 확정하지 않습니다.')
    cutoff=finished.astimezone(timezone.utc).isoformat()
    snapshot={'schema_version':1,'market':'COMBINED','collection_scope':'ALL_MARKETS_CAPS_TARGET_HISTORY',
        'history_selection':{'market':config.market,'decile':config.decile,'formation_days':config.formation_days},
        'history_symbols':sorted(set(common)&needed),'signal_date':day,'execution_date':sessions[idx+1],
        'sessions':sessions,'calendar_complete':True,'universe_complete':True,'data_cutoff_at':cutoff,
        'source':'KIS REST + KIS master','signal_price_basis':'KIS_ADJUSTED','quality':'OFFICIAL_CURRENT','securities':securities,'collection_exclusions':exclusions,'history_scope':'FORMATION_TARGET_AND_OWNED','shock_scope':'FORMATION_ONLY','market_cap_source':'KIS_MASTER','market_cap_tie_seed':tie_seed,
        'lineage':{'master':{k:v for k,v in master.items() if k!='rows'},'kis':market_data.lineage,'calendar':calendar['source']}}
    daily={'schema_version':1,'date':day,'sessions':sessions[:idx+1],'bars':daily_positions,'source':'KIS',
           'retrieved_at':cutoff,'quality':'OFFICIAL_CURRENT'}
    if bootstrap:
        snapshot.update(bootstrap_current=True,selection_basis='CURRENT_UNIVERSE_MASTER_CAP_PREVIOUS_CLOSE',prepared_at=cutoff)
        snapshot['lineage']['bootstrap']={'historical_membership':False,'market_cap_source':'KIS_MASTER','market_cap_actual_date':None,'regulatory_as_of':cutoff,'price_date':day}
    write_json(config.daily_path,daily)
    write_json(f'{config.data_dir}/snapshots/{day}.json',snapshot)
    weekly=date.fromisoformat(day).isocalendar()[:2]!=date.fromisoformat(sessions[idx+1]).isocalendar()[:2]
    # Price archives are refreshed at the execution window; selection is published separately.
    return {'date':day,'common_count':len(common),'d1_count':len(common)//10+bool(len(common)%10),
        'decile':config.decile,'decile_count':stop_rank-start_rank,'history_count':history_total,'weekly_signal_ready':False}
