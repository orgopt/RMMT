"""Read-only broker data preparation; never creates execution intents or orders."""
from datetime import datetime,date,timedelta,time
from .config import RMMTError,local_path
from .data import load_json,write_json,collect_calendar,collect_day
from .market import KisMarketData
from .strategy import SEOUL,compute_signal,rebalance_dates,execution_window

class Preparation:
    def __init__(self,config,broker,master=None,progress=None):
        self.config=config;self.broker=broker;self.master=master
        self.progress=progress or (lambda event:None)
        self.calendar=None;self.calendar_day=None;self.calendar_mtime=None;self.completed_key=None

    def valid_calendar(self,calendar,now):
        try:
            today=now.astimezone(SEOUL).date().isoformat();sessions=calendar['sessions']
            return (calendar['calendar_complete'] is True and calendar['source']=='KIS chk-holiday'
                and isinstance(sessions,list) and sessions==sorted(set(sessions))
                and all(date.fromisoformat(day).isoformat()==day for day in sessions)
                and calendar['start']<=today<calendar['end']
                and len([day for day in sessions if day<today])>=self.config.formation_days
                and calendar['start']<=self.config.rebalance_anchor_date
                and any(day>today for day in sessions))
        except (KeyError,ValueError,TypeError):return False

    def ensure_calendar(self,now=None):
        now=(now or datetime.now(SEOUL)).astimezone(SEOUL);today=now.date().isoformat()
        path=local_path(self.config.calendar_path)
        mtime=path.stat().st_mtime_ns if path.exists() else None
        if self.calendar_day==today and self.calendar_mtime==mtime and self.calendar is not None:return self.calendar
        calendar=None
        if path.exists():
            try:calendar=load_json(path)
            except RMMTError:pass
        if not self.valid_calendar(calendar or {},now):
            start=min((now.date()-timedelta(days=max(self.config.bootstrap_past_days,self.config.formation_days*2+30))).isoformat(),self.config.rebalance_anchor_date)
            end=(now.date()+timedelta(days=max(self.config.bootstrap_future_days,self.config.rebalance_days*2+30))).isoformat()
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

    def prepare_selection(self,calendar,now,ledger=None):
        today=now.date().isoformat()
        start,end=execution_window(today,self.config)
        if today not in rebalance_dates(calendar,self.config) or not start<=now<end:return False
        previous=[d for d in calendar['sessions'] if d<today]
        if not previous:raise RMMTError('선정 기준 직전 거래일 누락')
        day=previous[-1]
        snapshot=load_json(f'{self.config.data_dir}/snapshots/{day}.json')
        if (snapshot.get('signal_date')!=day or snapshot.get('execution_date')!=today
            or snapshot.get('quality')!='OFFICIAL_CURRENT' or not snapshot.get('lineage')):
            raise RMMTError('선정용 직전 거래일의 공식 스냅샷 필요')
        if local_path(self.config.snapshot_path).exists():
            existing=load_json(self.config.snapshot_path)
            if (existing.get('selection_date')==today and existing.get('selection_config_hash')==self.config.hash
                and existing.get('signal_date')==day):return True
        self.progress({'stage':'PREPARING_SELECTION','date':today})
        compute_signal(snapshot,self.config)
        snapshot.update(selection_date=today,selected_at=now.isoformat(),selection_config_hash=self.config.hash)
        if ledger is not None:snapshot['selection_positions']=ledger.positions()
        write_json(self.config.snapshot_path,snapshot)
        self.progress({'stage':'SELECTION_READY','date':today})
        return True

    def completed(self,today,positions):
        paths=[self.config.preparation_state_path,self.config.daily_path,f'{self.config.data_dir}/snapshots/{today}.json',self.config.snapshot_path]
        signature=[]
        for value in paths:
            path=local_path(value)
            try:stat=path.stat();signature.append((stat.st_mtime_ns,stat.st_size))
            except OSError:signature.append(None)
        key=(today,self.config.hash,tuple(sorted(positions)),tuple(signature))
        if self.completed_key==key:return True
        try:
            marker=load_json(self.config.preparation_state_path)
            if marker.get('day')!=today or marker.get('config_hash')!=self.config.hash or marker.get('owned_symbols')!=sorted(positions):return False
            daily=load_json(self.config.daily_path)
            archive=load_json(f'{self.config.data_dir}/snapshots/{today}.json')
            if not (daily.get('date')==today and daily.get('source')=='KIS' and daily.get('quality')=='OFFICIAL_CURRENT'
                and archive.get('signal_date')==today and archive.get('quality')=='OFFICIAL_CURRENT'
                and archive.get('universe_complete') is True and archive.get('lineage')):return False
            if not set(positions)<=set(daily.get('bars',{})):return False
            if marker.get('weekly_signal_ready'):
                signal=load_json(self.config.snapshot_path)
                if signal.get('signal_date')!=today or signal.get('quality')!='OFFICIAL_CURRENT':return False
            self.completed_key=key
            return True
        except RMMTError:return False

    def prepare_close(self,ledger,now=None,enabled=None):
        now=(now or datetime.now(SEOUL)).astimezone(SEOUL);today=now.date().isoformat()
        calendar=self.ensure_calendar(now)
        details=self.next_weekly(calendar,today)
        enabled=self.config.auto_collect if enabled is None else enabled
        if not enabled:return {'preparation':'AUTO_COLLECTION_DISABLED',**details}
        if today not in calendar['sessions']:
            return {'preparation':'NON_SESSION',**details}
        if now.time()<time.fromisoformat(self.config.data_ready_time):
            return {'preparation':'WAITING_DATA_CLOSE','ready_after':self.config.data_ready_time+' KST',**details}
        positions=ledger.positions()
        if not self.completed(today,positions):
            self.progress({'stage':'PREPARING_MARKET_DATA','date':today})
            source=KisMarketData(self.broker,progress=self.progress)
            result=collect_day(today,calendar,self.config,positions,self.master,source,progress=self.progress)
            daily=load_json(self.config.daily_path)
            if daily.get('date')!=today or daily.get('quality')!='OFFICIAL_CURRENT':raise RMMTError('수집 완료 후 공식 평가자료 확인 실패')
            # The collector must have published every required output before marking completion.
            archive=load_json(f'{self.config.data_dir}/snapshots/{today}.json')
            if archive.get('signal_date')!=today or not archive.get('universe_complete') or not archive.get('lineage'):
                raise RMMTError('수집 완료 후 공식 전체종목 스냅샷 확인 실패')
            with ledger.db:ledger.mark_daily(today,daily['bars'],daily['sessions'])
            write_json(self.config.preparation_state_path,{'day':today,'config_hash':self.config.hash,
                'owned_symbols':sorted(positions),'weekly_signal_ready':result['weekly_signal_ready']})
            self.progress({'stage':'MARKET_DATA_READY',**result})
        marker=load_json(self.config.preparation_state_path)
        return {'preparation':'WAITING_SELECTION',**details}
