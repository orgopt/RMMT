"""Read-only broker data preparation; never creates execution intents or orders."""
from datetime import datetime,date,timedelta,time
from .config import RMMTError,local_path
from .ledger import process_lock
from .data import load_json,write_json,collect_calendar,collect_day
from .market import KisMarketData
from .strategy import SEOUL,compute_signal,require_signal_data,rebalance_dates,execution_window

class Preparation:
    def __init__(self,config,broker,master=None,progress=None,clock=None):
        self.config=config;self.broker=broker;self.master=master
        self.progress=progress or (lambda event:None)
        self.clock=clock or (lambda:datetime.now(SEOUL))
        self.calendar=None;self.calendar_day=None;self.calendar_mtime=None;self.current_phase=None

    def valid_calendar(self,calendar,now):
        try:
            today=now.astimezone(SEOUL).date().isoformat();sessions=calendar['sessions']
            return (calendar['calendar_complete'] is True and calendar['source']=='KIS chk-holiday'
                and isinstance(sessions,list) and sessions==sorted(set(sessions))
                and all(date.fromisoformat(day).isoformat()==day for day in sessions)
                and calendar['start']<=today<calendar['end']
                and len([day for day in sessions if day<today])>=self.config.formation_days+1
                and (self.config.rebalance_days.upper().endswith('D') is False or self.config.rebalance_days.upper()=='1D' or calendar['start']<=date(now.year,1,1).isoformat())
                and any(day>today for day in sessions))
        except (KeyError,ValueError,TypeError):return False

    def ensure_calendar(self,now=None):
        now=(now or self.clock()).astimezone(SEOUL);today=now.date().isoformat()
        path=local_path(self.config.calendar_path)
        mtime=path.stat().st_mtime_ns if path.exists() else None
        if self.calendar_day==today and self.calendar_mtime==mtime and self.calendar is not None:return self.calendar
        calendar=None
        if path.exists():
            try:calendar=load_json(path)
            except RMMTError:pass
        if not self.valid_calendar(calendar or {},now):
            start=min((now.date()-timedelta(days=max(self.config.bootstrap_past_days,self.config.formation_days*2+30))).isoformat(),date(now.year,1,1).isoformat())
            from .config import parse_rebalance
            count,unit=parse_rebalance(self.config.rebalance_days)
            horizon=count*{'d':2,'w':7,'m':31,'y':366}[unit]+40
            end=(now.date()+timedelta(days=max(self.config.bootstrap_future_days,horizon))).isoformat()
            self.progress({'stage':'PREPARING_CALENDAR','start':start,'end':end})
            calendar=collect_calendar(self.broker,start,end,progress=self.progress)
            if not self.valid_calendar(calendar,now):raise RMMTError('KIS 거래일 달력 범위/과거21거래일/다음거래일 확인 실패')
            write_json(self.config.calendar_path,calendar)
        self.calendar=calendar;self.calendar_day=today;self.calendar_mtime=path.stat().st_mtime_ns
        self.progress({'stage':'CALENDAR_READY','sessions':len(calendar['sessions'])})
        return calendar

    def next_weekly(self,calendar,today):
        # Compatibility name: dates are now driven by the trading-day interval.
        upcoming=[d for d in rebalance_dates(calendar,self.config) if d>=today]
        return {'next_signal_date':upcoming[0],'next_execution_date':upcoming[0]} if upcoming else {}

    def phase(self,name,status,**details):
        self.current_phase=name
        self.progress({'stage':'PIPELINE_PROGRESS','phase':name,'status':status,**details})

    def prepare_archive(self,ledger,calendar,now=None):
        """At the configured collection time update the bundle without selecting or ordering."""
        now=(now or self.clock()).astimezone(SEOUL);today=now.date().isoformat()
        if not self.config.history_bundle_path or today not in calendar['sessions']:return False
        opening=datetime.combine(now.date(),time.fromisoformat(self.config.collection_time),SEOUL)
        start,_=execution_window(today,self.config)
        if not opening<=now<start:return False
        previous=[d for d in calendar['sessions'] if d<today]
        if not previous:raise RMMTError('증분 수집 기준 거래일 없음')
        from .history_bundle import HistoryBundle,HistoryCollector
        path=self.config.history_bundle_path
        if not local_path(path).exists():raise RMMTError('먼저 수집한 단일 KIS 일봉 파일을 싱크해야 합니다.')
        with process_lock(path+'.lock'):
            bundle=HistoryBundle(path)
            try:
                if bundle.get('status')=='READY' and bundle.get('asof_date')==previous[-1]:
                    try:
                        bundle.snapshot(previous[-1])
                        if bundle.get('validation',{}).get('formation_days')!=self.config.formation_days:
                            raise RMMTError('포메이션 변경 — 전체 원주가 수집 구간 재검증 필요')
                    except RMMTError:pass
                    else:
                        self.phase('COLLECTION','COMPLETE',day=today,detail='해당 기준일 KIS 통합 파일 준비 완료',server='KIS')
                        return True
                self.phase('COLLECTION','RUNNING',day=today,detail=self.config.collection_time+' KST 수집 일정 — 전체 보통주 1년 일봉 증분 갱신',server='KIS')
                def progress(value):
                    self.phase('COLLECTION','RUNNING',day=today,server='KIS',
                        **{k:v for k,v in value.items() if k in ('completed','total','symbol','errors','elapsed_seconds','eta_seconds')},
                        detail='자료 갱신 '+str(value.get('stage','')))
                result=HistoryCollector(self.config,self.broker,bundle,progress,deadline=start,clock=self.clock).run(previous[-1])
                if not result['snapshot_ready']:
                    raise RMMTError('KIS 전 종목 수집 미완성: 수정주가 '+str(result.get('year_gap_count',0))
                        +'종목 / 원주가 '+str(result.get('original_gap_count',0))+'종목 / 조회 오류 '
                        +str(len(result.get('request_errors',[])))+'종목 — 재조회 대기')
                self.phase('COLLECTION','COMPLETE',day=today,completed=result['symbols'],total=result['symbols'],detail='증분 갱신 완료 — 선정 시각 대기')
                return True
            except RMMTError as exc:
                self.phase('COLLECTION','ERROR',day=today,error=str(exc),retry_after=self.config.error_retry_seconds)
                raise
            finally:bundle.close()

    def prepare_from_archive(self,ledger,calendar,now):
        today=now.date().isoformat();start,end=execution_window(today,self.config)
        previous=[d for d in calendar['sessions'] if d<today];day=previous[-1]
        from .history_bundle import HistoryBundle
        bundle=HistoryBundle(self.config.history_bundle_path,readonly=True)
        try:snapshot=bundle.snapshot(day)
        finally:bundle.close()
        # Calendars may have different history lengths, but formation/execution must agree.
        cutoff=calendar['sessions'].index(day)-self.config.formation_days
        if cutoff<0:raise RMMTError('KIS 통합 스냅샷 검증에 필요한 포메이션 달력 부족')
        required=calendar['sessions'][cutoff:calendar['sessions'].index(today)+1]
        if [d for d in snapshot['sessions'] if required[0]<=d<=today]!=required:
            raise RMMTError('KIS 통합 파일과 실행 달력의 포메이션 거래일 불일치')
        snapshot=dict(snapshot,sessions=calendar['sessions'])
        self.phase('COLLECTION','COMPLETE',day=today,detail=self.config.collection_time+' KST 수집 일정의 KIS 통합 파일 사용 — 추가 조회 없음',server='KIS')
        self.phase('VALIDATION','RUNNING',day=today)
        require_signal_data(snapshot,self.config)
        positions=ledger.positions();rows={r['symbol']:r for r in snapshot['securities']}
        if not set(positions)<=set(rows):raise RMMTError('KIS 통합 파일에 귀속 보유종목 평가자료 누락')
        daily={'date':day,'quality':'OFFICIAL_CURRENT','source':'KIS','sessions':[d for d in calendar['sessions'] if d<=day],
            'bars':{s:{b['date']:{'date':b['date'],'nominal_close':b['nominal_close'],'volume':b['volume']} for b in rows[s]['daily']} for s in positions}}
        if self.clock()>=end:raise RMMTError('KIS 통합 자료 검증 중 주문창 종료')
        write_json(self.config.daily_path,daily)
        write_json(f'{self.config.data_dir}/snapshots/{day}.json',snapshot)
        self.phase('VALIDATION','COMPLETE',day=today)
        return self.prepare_selection(calendar,self.clock(),ledger)

    def skip_unready_cycle(self,ledger,calendar,at):
        """Persist a missed cycle only after the entire execution window expires."""
        at=at.astimezone(SEOUL);day=at.date().isoformat()
        if day not in rebalance_dates(calendar,self.config):return False
        start,end=execution_window(day,self.config)
        if at<end:return False
        if ledger.blocking_run(day):return False
        run_id=f'{self.config.strategy_id}:{day}:REBALANCE'
        ledger.begin_run(run_id,day,'REBALANCE',{'reason':'DATA_NOT_READY_BEFORE_DEADLINE','scheduled_at':start.isoformat()})
        ledger.finish_run(run_id,'SKIPPED')
        self.phase(self.current_phase or 'COLLECTION','TIMEOUT',day=day,detail='주문창 종료 — 신규 주문 없음')
        self.progress({'state':'SKIPPED','day':day,'reason':'DATA_NOT_READY_BEFORE_DEADLINE'})
        return True

    def prepare_rebalance(self,ledger,calendar,now=None):
        """Refresh prior-session inputs at the scheduled start; failures remain retryable."""
        now=(now or self.clock()).astimezone(SEOUL);today=now.date().isoformat()
        start,end=execution_window(today,self.config)
        if today not in rebalance_dates(calendar,self.config) or not start<=now<end:return False
        if ledger.blocking_run(today):return False
        if self.config.history_bundle_path:
            try:return self.prepare_from_archive(ledger,calendar,now)
            except RMMTError as exc:
                self.phase(self.current_phase or 'VALIDATION','ERROR',day=today,error=str(exc),retry_after=self.config.error_retry_seconds)
                raise
        previous=[d for d in calendar['sessions'] if d<today]
        if not previous:raise RMMTError('증분 수집에 필요한 직전 거래일 없음')
        day=previous[-1]
        phase='COLLECTION'
        self.phase(phase,'RUNNING',day=today,detail=f'직전 거래일 {day}까지 증분 갱신')
        def collection_progress(value):
            if self.clock()>=end:raise RMMTError('자료 수집 중 주문창 종료: 신규 주문 없음')
            self.phase('COLLECTION','RUNNING',day=today,
                **{k:v for k,v in value.items() if k in ('completed','total','symbol','server')},
                detail={'KIS_ALL_COMMON_CAP':'종목명부·시총 확인','KIS_HISTORY':'원주가 이력 확인',
                        'KIS_ADJUSTED_HISTORY':'수정주가 이력 확인',
                        'PREPARING_SIGNAL_HISTORY':'가격 이력 갱신'}.get(value.get('stage'),'자료 갱신'))
        try:
            source=KisMarketData(self.broker,progress=collection_progress)
            collect_day(day,calendar,self.config,ledger.positions(),self.master,source,
                progress=collection_progress,bootstrap=True,deadline=end)
            self.phase('COLLECTION','COMPLETE',day=today,detail=f'{day}까지 자료 조회 완료')
            phase='VALIDATION';self.phase(phase,'RUNNING',day=today)
            snapshot=load_json(f'{self.config.data_dir}/snapshots/{day}.json')
            if snapshot.get('quality')!='OFFICIAL_CURRENT' or not snapshot.get('lineage'):
                raise RMMTError('공식 출처·계보 없는 선정 자료')
            try:
                verified=require_signal_data(snapshot,self.config,progress=lambda counts:self.phase('VALIDATION','RUNNING',day=today,**counts))
            except RMMTError as exc:
                snapshot.update(quality='DATA_INCOMPLETE',signal_inputs_complete=False,input_error=str(exc))
                write_json(f'{self.config.data_dir}/snapshots/{day}.json',snapshot)
                raise
            daily=load_json(self.config.daily_path)
            if daily.get('date')!=day or daily.get('quality')!='OFFICIAL_CURRENT' or not set(ledger.positions())<=set(daily.get('bars',{})):
                raise RMMTError('직전 거래일 보유종목 평가자료 미완성')
            if self.clock()>=end:raise RMMTError('자료 검증 중 주문창 종료: 신규 주문 없음')
            snapshot['signal_inputs_complete']=True
            write_json(f'{self.config.data_dir}/snapshots/{day}.json',snapshot)
            self.phase(phase,'COMPLETE',day=today,completed=verified['decile_size'],total=verified['decile_size'])
            phase='SELECTION'
            return self.prepare_selection(calendar,self.clock(),ledger)
        except RMMTError as exc:
            self.phase(phase,'ERROR',day=today,error=str(exc),retry_after=self.config.error_retry_seconds)
            raise

    def prepare_selection(self,calendar,now,ledger=None):
        today=now.date().isoformat()
        start,end=execution_window(today,self.config)
        if today not in rebalance_dates(calendar,self.config) or not start<=now<end:return False
        if ledger is not None and ledger.blocking_run(today):return False
        previous=[d for d in calendar['sessions'] if d<today]
        if not previous:raise RMMTError('선정 기준 직전 거래일 누락')
        day=previous[-1]
        snapshot=load_json(f'{self.config.data_dir}/snapshots/{day}.json')
        if (snapshot.get('signal_date')!=day or snapshot.get('execution_date')!=today
            or snapshot.get('quality')!='OFFICIAL_CURRENT' or not snapshot.get('lineage')):
            raise RMMTError('선정용 직전 거래일의 공식 스냅샷 필요')
        require_signal_data(snapshot,self.config)
        self.phase('SELECTION','RUNNING',day=today)
        signal=compute_signal(snapshot,self.config,progress=lambda counts:self.phase('SELECTION','RUNNING',day=today,**counts))
        # Computation and UI refresh time also count toward the order window.
        if self.clock()>=end:raise RMMTError('종목선정 중 주문창 종료: 신규 주문 없음')
        snapshot.update(selection_date=today,selected_at=now.isoformat(),selection_config_hash=self.config.hash)
        if ledger is not None:snapshot['selection_positions']=ledger.positions()
        write_json(self.config.snapshot_path,snapshot)
        from collections import Counter
        counts=Counter(reason for item in signal['decisions'] for reason in item['reasons'])
        selected=sum(row['selected'] for row in signal['candidates'])
        self.phase('SELECTION','COMPLETE',day=today,completed=signal['decile_size'],total=signal['decile_size'],
            detail=f'{selected}종목 선정 / 제외 {len(signal["decisions"])-len(signal["candidates"])}종목',exclusions=dict(counts))
        return True
