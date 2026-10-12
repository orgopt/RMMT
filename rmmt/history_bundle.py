"""Standalone KIS history archive. One SQLite file; no execution ledger or orders."""
from datetime import date,datetime,timedelta,timezone
from dataclasses import replace
from pathlib import Path
import base64,hashlib,json,secrets,sqlite3,time,zlib

from .config import RMMTError,local_path
from .data import load_json,MasterProvider,status_from_master
from .market import KisMarketData
from .strategy import SEOUL,decimal

COLLECTION_CONTRACT_VERSION=2

def validate_history_bars(bars,mode,required):
    field='adjusted_close' if mode=='0' else 'nominal_close'
    missing=[d for d in required if d not in bars];invalid=[]
    for d in required:
        if d not in bars:continue
        try:
            price=decimal(bars[d][field]);volume=decimal(bars[d]['volume'])
            if (price<0 or volume<0 or volume!=volume.to_integral_value()
                or (volume>0 and price<=0) or (mode=='1' and price!=price.to_integral_value())):
                raise ValueError()
        except (RMMTError,KeyError,TypeError,ValueError):invalid.append(d)
    return {'complete':not missing and not invalid,'expected':len(required),'observed':len(bars),
            'missing_dates':missing,'invalid_dates':invalid}


def encode(value):
    return zlib.compress(json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str).encode())

def decode(value):
    return json.loads(zlib.decompress(value))

def year_start(day):
    end=date.fromisoformat(day)
    try:return end.replace(year=end.year-1).isoformat()
    except ValueError:return end.replace(year=end.year-1,day=28).isoformat()


class HistoryBundle:
    def __init__(self,path,readonly=False):
        self.path=local_path(path)
        if readonly:
            if not self.path.exists():raise RMMTError('KIS 통합 일봉 파일이 없습니다.')
            from urllib.parse import quote
            self.db=sqlite3.connect('file:'+quote(str(self.path))+'?mode=ro',uri=True,timeout=30)
        else:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self.db=sqlite3.connect(self.path,timeout=30)
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS histories(symbol TEXT,mode TEXT,start TEXT,asof TEXT,
                    data BLOB NOT NULL,validation BLOB NOT NULL,PRIMARY KEY(symbol,mode));
                CREATE TABLE IF NOT EXISTS raw(hash TEXT PRIMARY KEY,symbol TEXT,mode TEXT,
                    start TEXT,end TEXT,retrieved_at TEXT,data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS failures(symbol TEXT PRIMARY KEY,reason TEXT,updated_at TEXT);
            ''')
            self.db.commit()
    def close(self):self.db.close()
    def get(self,key,default=None):
        try:
            row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            return decode(row[0]) if row else default
        except (sqlite3.DatabaseError,zlib.error,ValueError,TypeError):
            raise RMMTError('KIS 통합 파일 읽기·형식 오류 — 파일 싱크 상태 확인 필요') from None
    def put(self,key,value):
        self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encode(value)))
    def history(self,symbol,mode):
        try:
            row=self.db.execute('SELECT start,asof,data,validation FROM histories WHERE symbol=? AND mode=?',(symbol,mode)).fetchone()
            return {'start':row[0],'asof':row[1],'bars':decode(row[2]),'validation':decode(row[3])} if row else None
        except (sqlite3.DatabaseError,zlib.error,ValueError,TypeError):
            raise RMMTError(symbol+' KIS 통합 일봉 읽기·형식 오류 — 재검증 필요') from None
    def save_history(self,symbol,mode,start,day,bars,validation):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO histories VALUES(?,?,?,?,?,?)',(symbol,mode,start,day,encode(bars),encode(validation)))
    def snapshot(self,day):
        if self.get('status')!='READY' or self.get('asof_date')!=day:
            raise RMMTError('KIS 통합 일봉 자료 미완성 또는 기준일 불일치 — 수집 결과 확인 필요')
        report=self.get('validation',{})
        if (report.get('collection_contract_version')!=COLLECTION_CONTRACT_VERSION
            or report.get('asof_date')!=day or report.get('collection_complete') is not True
            or report.get('snapshot_ready') is not True or report.get('year_gaps')
            or report.get('original_gaps') or report.get('request_errors')):
            raise RMMTError('KIS 전 종목 수집 완전성 재검증 필요 — 수집을 다시 실행하세요.')
        value=self.get('snapshot')
        if not value or value.get('signal_date')!=day:raise RMMTError('KIS 통합 파일의 검증된 스냅샷 없음')
        return value


class HistoryCollector:
    def __init__(self,config,broker,bundle,progress=None,deadline=None,clock=None):
        self.config=config;self.broker=broker;self.bundle=bundle
        self.progress=progress or (lambda value:None)
        self.deadline=deadline;self.clock=clock or (lambda:datetime.now(SEOUL))
        self.source=KisMarketData(broker,progress=lambda value:self.check_deadline())
        self.source._archive=self.archive
        self.extra={};self.requests=0

    def check_deadline(self):
        if self.deadline is not None and self.clock()>=self.deadline:raise RMMTError('일봉 수집이 선정 시각까지 미완료 — 매매 차단')

    def archive(self,symbol,start,end,body,mode='1'):
        encoded=json.dumps(body,sort_keys=True).encode();digest=hashlib.sha256(encoded).hexdigest()
        at=datetime.now(timezone.utc).isoformat()
        with self.bundle.db:
            self.bundle.db.execute('INSERT OR IGNORE INTO raw VALUES(?,?,?,?,?,?,?)',
                (digest,symbol,mode,start,end,at,zlib.compress(encoded)))
        self.extra={}
        for row in body['output2']:
            compact=row.get('stck_bsop_date','')
            if not compact:continue
            day=compact[:4]+'-'+compact[4:6]+'-'+compact[6:]
            values={name:str(decimal(row[key])) for name,key in [('open','stck_oprc'),('high','stck_hgpr'),('low','stck_lwpr')]}
            close=decimal(row['stck_clpr']);volume=decimal(row['acml_vol'])
            if volume>0 and (min(decimal(v) for v in values.values())<=0 or close<=0
                or decimal(values['low'])>min(decimal(values['open']),close)
                or decimal(values['high'])<max(decimal(values['open']),close)):
                raise RMMTError(symbol+' KIS 일봉 OHLC 범위 오류')
            values.update(turnover=str(decimal(row.get('acml_tr_pbmn','0'))),revision_rate=str(row.get('prtt_rate','0')),
                          source_hash=digest)
            self.extra[day]=values

    def fetch(self,symbol,start,end,mode):
        for attempt in range(4):
            self.check_deadline()
            try:
                self.requests+=1
                rows=self.source._daily(symbol,start,end,price_mode=mode)
                self.check_deadline()
                return {d:bar|self.extra.get(d,{}) for d,bar in rows.items()}
            except (RMMTError,KeyError,TypeError,ValueError) as exc:
                if attempt==3:raise RMMTError(f'{symbol} KIS 일봉 조회 실패: {str(exc)[:180]}') from None
                self.progress({'stage':'RETRY','symbol':symbol,'mode':mode,'attempt':attempt+1,'error':str(exc)[:180]})
                time.sleep(min(15,2**attempt))

    def calendar(self,start,end):
        old=self.bundle.get('calendar')
        if not old and local_path(self.config.calendar_path).exists():
            try:old=load_json(self.config.calendar_path)
            except RMMTError:pass
        known={r['bass_dt']:r for r in (old or {}).get('raw',[]) if isinstance(r,dict) and r.get('opnd_yn') in ('Y','N')}
        sessions=set((old or {}).get('sessions',[]))
        # A certified contiguous cached calendar also certifies its closed dates.
        valid_old=(old or {}).get('calendar_complete') is True and (old or {}).get('source')=='KIS chk-holiday'
        current=date.fromisoformat(start);last=date.fromisoformat(end);raw=[];completed=0
        while current<=last:
            self.check_deadline()
            day=current.isoformat();compact=current.strftime('%Y%m%d')
            row=known.get(compact)
            if row is None and valid_old and old['start']<=day<=old['end']:
                row={'bass_dt':compact,'opnd_yn':'Y' if day in sessions else 'N'}
            if row is None:
                for attempt in range(4):
                    try:
                        body,_=self.broker.request('GET','/uapi/domestic-stock/v1/quotations/chk-holiday','CTCA0903R',
                            {'BASS_DT':compact,'CTX_AREA_NK':'','CTX_AREA_FK':''})
                        matches=[r for r in body['output'] if r.get('bass_dt')==compact]
                        if len(matches)!=1 or matches[0].get('opnd_yn') not in ('Y','N'):raise RMMTError('공식 달력 응답 미완성')
                        row=matches[0];break
                    except (RMMTError,KeyError,TypeError,AttributeError):
                        if attempt==3:raise RMMTError('공식 거래일 달력 조회 실패: '+day) from None
                        time.sleep(2**attempt)
            raw.append(row);completed+=1
            if completed%30==0:self.progress({'stage':'CALENDAR','completed':completed,'total':(last-date.fromisoformat(start)).days+1})
            current+=timedelta(days=1)
        result={'start':start,'end':end,'sessions':[r['bass_dt'][:4]+'-'+r['bass_dt'][4:6]+'-'+r['bass_dt'][6:] for r in raw if r['opnd_yn']=='Y'],
            'raw':raw,'source':'KIS chk-holiday','calendar_complete':True,'retrieved_at':datetime.now(timezone.utc).isoformat()}
        with self.bundle.db:self.bundle.put('calendar',result)
        return result

    def collect_history(self,symbol,mode,start,day,required):
        cached=self.bundle.history(symbol,mode)
        bars={};cursor=day;query_start=start
        # Check actual coverage, including incomplete same-day retries.
        if cached and cached['start']<=start and cached['asof']<=day:
            bars={d:b for d,b in cached['bars'].items() if start<=d<=day}
            coverage=validate_history_bars(bars,mode,required)
            if cached['asof']==day and coverage['complete']:return bars
            bars={d:b for d,b in bars.items() if d not in coverage['invalid_dates']}
            query_start=max(start,(date.fromisoformat(cached['asof'])-timedelta(days=self.config.history_overlap_days)).isoformat())
            missing=coverage['missing_dates']+coverage['invalid_dates']
            if missing:query_start=min(query_start,min(missing))
        for _ in range(self.config.history_max_pages):
            batch=self.fetch(symbol,query_start,cursor,mode)
            if not batch:break
            field='adjusted_close' if mode=='0' else 'nominal_close'
            revised=any(decimal(b[field])!=decimal(bars[d][field]) for d,b in batch.items() if d in bars)
            if mode=='0' and revised:
                # Restart from the newest page with one consistent adjustment anchor.
                bars={};query_start=start;cursor=day
                continue
            bars.update(batch);oldest=min(batch)
            if validate_history_bars(bars,mode,required)['complete'] or oldest<=query_start:break
            next_cursor=(date.fromisoformat(oldest)-timedelta(days=1)).isoformat()
            if next_cursor>=cursor:raise RMMTError(symbol+' KIS 일봉 과거 커서 역행 실패')
            cursor=next_cursor
        else:raise RMMTError(symbol+' KIS 일봉 페이지 한도 초과')
        validation=validate_history_bars(bars,mode,required)
        self.bundle.save_history(symbol,mode,start,day,bars,validation)
        return bars

    def run(self,day=None):
        now=datetime.now(SEOUL)
        # An open market's daily bar is never used, even if an API happens to return it.
        tentative=(now.date()-timedelta(days=1)).isoformat()
        end=(now.date()+timedelta(days=40)).isoformat()
        start=year_start(day or tentative)
        with self.bundle.db:
            self.bundle.put('status','COLLECTING');self.bundle.put('started_at',now.isoformat());self.bundle.put('contract_version',1)
        calendar=self.calendar(start,end)
        prior=[d for d in calendar['sessions'] if d<now.date().isoformat()]
        day=day or prior[-1]
        if day>=now.date().isoformat() or day not in calendar['sessions']:raise RMMTError('수집 기준일은 직전 완료 거래일 이하여야 합니다.')
        start=year_start(day)
        if start<calendar['start']:calendar=self.calendar(start,end)
        sessions=[d for d in calendar['sessions'] if start<=d<=day]
        index=calendar['sessions'].index(day)
        if index<self.config.formation_days:raise RMMTError('1년 자료의 거래일 수보다 포메이션이 깁니다. 필요한 F+1 구간을 먼저 확보해야 합니다.')
        formation=calendar['sessions'][index-self.config.formation_days:index+1]
        master,_=MasterProvider(config=self.config).get()
        originals=[]
        for path in master['raw_paths']:
            content=local_path(path).read_bytes()
            originals.append({'market':'KOSDAQ' if 'KOSDAQ_' in path else 'KOSPI',
                'sha256':hashlib.sha256(content).hexdigest(),'content_base64':base64.b64encode(content).decode()})
        master=dict(master,rows={s:r for s,r in master['rows'].items() if r['listing_date']<=day.replace('-','')})
        caps=self.source.caps(day,master,bootstrap=True)
        symbols=sorted(caps)
        with self.bundle.db:
            self.bundle.put('master_originals',originals)
            for key,value in [('asof_date',day),('history_start',start),('master',master),('caps',caps),('config',self.config.public()),('symbols',symbols)]:self.bundle.put(key,value)
        self.progress({'stage':'START','asof_date':day,'start':start,'total':len(symbols),'sessions':len(sessions)})
        begin=time.monotonic();errors=[]
        for completed,symbol in enumerate(symbols,1):
            self.check_deadline()
            row=master['rows'][symbol];listed=row['listing_date'];listing=listed[:4]+'-'+listed[4:6]+'-'+listed[6:]
            try:
                required=[d for d in sessions if d>=listing]
                self.collect_history(symbol,'0',max(start,listing),day,required)
                # Original prices/volumes are required only for current formation/valuation.
                original_required=[d for d in formation if d>=listing]
                self.collect_history(symbol,'1',max(formation[0],listing),day,original_required)
                incomplete=[mode for mode in ('0','1') if not self.bundle.history(symbol,mode)['validation']['complete']]
                if incomplete:raise RMMTError(symbol+' KIS 일봉 날짜 누락·가격/거래량 오류: 모드 '+','.join(incomplete)+' — 재조회 필요')
                with self.bundle.db:self.bundle.db.execute('DELETE FROM failures WHERE symbol=?',(symbol,))
            except (RMMTError,ValueError,TypeError,KeyError) as exc:
                errors.append({'symbol':symbol,'reason':str(exc)[:220]})
                with self.bundle.db:self.bundle.db.execute('INSERT OR REPLACE INTO failures VALUES(?,?,?)',(symbol,str(exc)[:220],datetime.now(timezone.utc).isoformat()))
            if completed%10==0 or completed==len(symbols):
                self.progress({'stage':'HISTORY','completed':completed,'total':len(symbols),'errors':len(errors),'requests':self.requests,
                    'elapsed_seconds':round(time.monotonic()-begin),'eta_seconds':round((time.monotonic()-begin)/completed*(len(symbols)-completed))})
        self.check_deadline()
        return self.finalize(day,calendar,master,caps,symbols,formation,errors)

    def finalize(self,day,calendar,master,caps,symbols,formation,errors):
        now=datetime.now(timezone.utc);cutoff=now.isoformat();securities=[];year_gaps=[];original_gaps=[]
        history_start=self.bundle.get('history_start')
        year_sessions=[d for d in calendar['sessions'] if history_start<=d<=day]
        prior=self.bundle.get('snapshot',{})
        tie_seed=prior.get('market_cap_tie_seed') if prior.get('signal_date')==day else None
        tie_seed=tie_seed or secrets.token_hex(16)
        for symbol in symbols:
            m=master['rows'][symbol];a=self.bundle.history(symbol,'0');o=self.bundle.history(symbol,'1')
            adjusted=(a or {}).get('bars',{});original=(o or {}).get('bars',{})
            listed=m['listing_date'];listing=listed[:4]+'-'+listed[4:6]+'-'+listed[6:]
            for mode,history,bars,required,gaps,begin in (
                ('0',a,adjusted,[d for d in year_sessions if d>=listing],year_gaps,max(history_start,listing)),
                ('1',o,original,[d for d in formation if d>=listing],original_gaps,max(formation[0],listing))):
                validation=validate_history_bars(bars,mode,required)
                if not history or history['asof']!=day or history['start']>begin:
                    validation['complete']=False;validation['coverage_error']='기준일 또는 수집 시작일 불일치'
                if not validation['complete']:gaps.append({'symbol':symbol,**validation})
            daily=[{'date':d,'adjusted_close':adjusted.get(d,{}).get('adjusted_close'),
                    'nominal_close':original.get(d,{}).get('nominal_close'),'volume':original.get(d,{}).get('volume')} for d in formation]
            securities.append({'symbol':symbol,'name':m['name'],'market':m['market'],'security_type':'COMMON',
                'market_cap':str(caps[symbol]['market_cap']),'market_cap_date':day,'market_cap_source':'KIS_MASTER',
                'market_cap_basis':'CURRENT_MASTER_APPROX','market_cap_actual_date':None,'classification_source':'KIS master',
                'listing_date':m['listing_date'],'signal_source':'KIS','adjustment_basis':'KIS_ADJUSTED',
                'shock_history_complete':all(b['adjusted_close'] is not None and decimal(b['adjusted_close'])>0 for b in daily),
                'tradable_on_signal':not m['halted'] and not m['liquidating'],
                'regulatory':status_from_master(m,master,now),'daily':daily})
        snapshot={'schema_version':1,'market':'COMBINED','collection_scope':'ALL_MARKETS_FULL_HISTORY','signal_date':day,
            'execution_date':calendar['sessions'][calendar['sessions'].index(day)+1],'sessions':calendar['sessions'],
            'data_cutoff_at':cutoff,'calendar_complete':True,'universe_complete':True,'quality':'OFFICIAL_CURRENT',
            'source':'KIS REST + KIS master','signal_price_basis':'KIS_ADJUSTED','history_scope':'ONE_YEAR_ADJUSTED_ARCHIVE_FORMATION_ORIGINAL',
            'market_cap_tie_seed':tie_seed,
            'bootstrap_current':True,'securities':securities,
            'lineage':{'master':{k:v for k,v in master.items() if k!='rows'}|{'bundle_record':'meta/master_originals'},'calendar':calendar['source'],
                'bundle':{'schema_version':1,'price_modes':['0','1'],'raw_responses':self.bundle.db.execute('SELECT count(*) FROM raw').fetchone()[0]}}}
        collection_complete=bool(symbols) and not errors and not year_gaps and not original_gaps
        report={'asof_date':day,'history_start':self.bundle.get('history_start'),'symbols':len(symbols),'adjusted_sessions':len([d for d in calendar['sessions'] if self.bundle.get('history_start')<=d<=day]),
            'collection_contract_version':COLLECTION_CONTRACT_VERSION,'formation_days':self.config.formation_days,
            'request_errors':errors,'year_gap_count':len(year_gaps),'year_gaps':year_gaps,
            'original_gap_count':len(original_gaps),'original_gaps':original_gaps,
            'collection_complete':collection_complete,'snapshot_ready':collection_complete,
            'completed_at':cutoff,'current_universe_not_historical_membership':True}
        with self.bundle.db:
            if collection_complete:self.bundle.put('snapshot',snapshot)
            self.bundle.put('validation',report)
            self.bundle.put('status','READY' if report['snapshot_ready'] else 'INCOMPLETE')
        self.progress({'stage':'COMPLETE','asof_date':day,'symbols':len(symbols),'year_gap_count':len(year_gaps),
            'original_gap_count':len(original_gaps),'request_errors':len(errors),
            'collection_complete':collection_complete,'snapshot_ready':report['snapshot_ready']})
        return report
