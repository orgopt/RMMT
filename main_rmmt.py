#!/usr/bin/env python3
"""RMMT standalone entrypoint. Default invocation starts the configured trading service."""
import argparse,json,sys,time,os
from datetime import datetime,date,timedelta,timezone
from dataclasses import replace
from decimal import Decimal
from rmmt.config import ROOT,RMMTError,load_config,local_path,preview_holdings
from rmmt.data import load_json,write_json,LiveQuotes,collect_calendar,collect_day
from rmmt.strategy import compute_signal,plan_rebalance,timestamp,SEOUL,execution_window
PROGRAM_STARTED_AT=datetime.now(SEOUL)
from rmmt.ledger import Ledger,process_lock
from rmmt.kis import KIS
from rmmt.execution import Executor,PaperBroker
from rmmt.reporting import export_report
from rmmt.market import KisMarketData


def parser():
    p=argparse.ArgumentParser(description='독립 KIS 모멘텀/역모멘텀 실행기; .env의 STRATEGY로 전략 선택; 인자 없이 설정된 운용 서비스 및 대시보드 시작')
    p.add_argument('--env',default='.env',help='RMMT 안의 환경파일')
    sub=p.add_subparsers(dest='command')
    sub.add_parser('check',help='설정 확인: 접속/주문/원장 생성 없음')
    for name in ('signal','plan','paper'):
        s=sub.add_parser(name)
        s.add_argument('--snapshot',default=None)
        if name!='signal':
            s.add_argument('--quotes',default=None,help='시점·경고상태 포함 현재가 JSON')
            s.add_argument('--at',required=True,help='timezone 포함 평가시각')
            s.add_argument('--db',default=None)
    s=sub.add_parser('init-ledger');s.add_argument('--db',default=None)
    s=sub.add_parser('status');s.add_argument('--db',default=None);s.add_argument('--json',action='store_true',help='대시보드 대신 JSON 출력')
    s=sub.add_parser('dashboard',help='원장 읽기 전용 터미널 대시보드; KIS 접속 없음')
    s.add_argument('--db',default=None);s.add_argument('--snapshot',default=None);s.add_argument('--quotes',default=None)
    s.add_argument('--watch',action='store_true',help='설정한 주기로 로컬 자료를 반복 조회')
    s=sub.add_parser('calendar');s.add_argument('--start',required=True);s.add_argument('--end',required=True)
    s.add_argument('--output',default=None)
    s=sub.add_parser('collect');s.add_argument('--date',required=True);s.add_argument('--calendar',default=None);s.add_argument('--db',default=None)
    s=sub.add_parser('broker-check',help='명시적 KIS 읽기 접속; 주문 없음');s.add_argument('--db',default=None)
    for name in ('run','service','reconcile'):
        s=sub.add_parser(name);s.add_argument('--db',default=None)
        s.add_argument('--snapshot',default=None)
        s.add_argument('--daily',default=None)
        s.add_argument('--calendar',default=None)
        if name=='service':
            s.add_argument('--auto-collect',action='store_true',default=None,help='호환 옵션: 예정 리밸런싱의 증분 수집은 항상 수행')
    s=sub.add_parser('cash-event');s.add_argument('--db',default=None);s.add_argument('--id',required=True)
    s.add_argument('--type',choices=['DIVIDEND','FEE_ADJUSTMENT','RECOVERY'],required=True);s.add_argument('--amount',required=True,help='변경할 투자상한(원); 만원 단위 양의 정수 금액')
    s=sub.add_parser('reconcile-cost');s.add_argument('--db',default=None);s.add_argument('--intent',required=True);s.add_argument('--fee',required=True);s.add_argument('--tax',required=True)
    s=sub.add_parser('corporate-action');s.add_argument('--db',default=None);s.add_argument('--id',required=True)
    s.add_argument('--symbol',required=True);s.add_argument('--quantity',required=True,type=int);s.add_argument('--evidence',required=True)
    s=sub.add_parser('bind-order',help='UNKNOWN 주문을 증권사 조회 결과로 명시 귀속')
    s.add_argument('--db',default=None);s.add_argument('--intent',required=True);s.add_argument('--broker-id',required=True);s.add_argument('--branch',required=True)
    s=sub.add_parser('set-capital',help='초기자산/현금을 바꾸지 않고 승인한 투자상한 변경')
    s.add_argument('--db',default=None);s.add_argument('--amount',required=True,help='변경할 투자상한(원); 만원 단위 양의 정수 금액')
    s=sub.add_parser('rearm-empty',help='오늘 주문 없는 COMPLETE 리밸런싱을 감사 기록 후 한 번 재예약; 주문 없음')
    s.add_argument('--db',default=None);s.add_argument('--date',required=True);s.add_argument('--at',required=True,help='당일만 적용할 HH:MM(KST), 09:00~15:00')
    s.add_argument('--reason',required=True)
    return p

def print_json(value,console=True):
    from rmmt.logs import current
    text=json.dumps(value,ensure_ascii=False,indent=2,default=str)
    logger=current()
    if logger:
        text=logger.redact(text);logger.write('screen',text);logger.write('system',text)
    if console:print(text)
def db_path(args,config):return args.db or config.database_path()

def apply_daily(ledger,path,calendar,day,required=True):
    if not local_path(path).exists():
        if required and ledger.positions():raise RMMTError('보유종목 마감 평가자료 없음')
        return
    daily=load_json(path)
    previous=[d for d in calendar['sessions'] if d<day]
    if not previous or daily['date']!=previous[-1] or daily.get('quality')!='OFFICIAL_CURRENT':
        raise RMMTError('최신 완료 거래일의 공식 평가자료가 필요합니다.')
    if daily['date']>=day or daily['sessions'][-1]!=daily['date']:raise RMMTError('평가일/거래일 배열 오류')
    ledger.mark_daily(daily['date'],daily['bars'],daily['sessions'])

def ready_signal(path,calendar,day,config):
    if not local_path(path).exists():return None,None
    snapshot=load_json(path)
    if snapshot['execution_date']!=day:return None,snapshot
    selected_at=timestamp(snapshot['selected_at']) if snapshot.get('selected_at') else None
    if (not selected_at or selected_at.astimezone(SEOUL).date().isoformat()!=day
        or selected_at.astimezone(SEOUL).time()<datetime.strptime(config.execution_time,'%H:%M').time()
        or snapshot.get('selection_config_hash')!=config.hash):
        raise RMMTError('당일 설정 시각 이후 현재 설정으로 완료한 종목선정 자료가 필요합니다.')
    if snapshot.get('quality')!='OFFICIAL_CURRENT' or not snapshot.get('lineage'):
        raise RMMTError('LIVE 신호는 공식 KIS 수정주가 출처·계보가 필요합니다.')
    if snapshot['sessions'] != calendar['sessions']:
        # Calendar can be extended; require identical past sessions through execution.
        old=[d for d in snapshot['sessions'] if d<=day];new=[d for d in calendar['sessions'] if d<=day]
        if old!=new:raise RMMTError('신호/실행 달력 불일치')
    cutoff=timestamp(snapshot['data_cutoff_at'])
    if snapshot['signal_date']>=day or cutoff>selected_at:
        raise RMMTError('선정 자료는 직전 거래일까지이며 조회 완료 후 선정해야 합니다.')
    from rmmt.strategy import require_signal_data
    return require_signal_data(snapshot,config),snapshot

def main(argv=None):
    cli=parser();args=cli.parse_args(argv)
    if args.command is None:args=cli.parse_args(['--env',args.env,'service'])
    config,credentials=load_config(args.env)
    from rmmt.logs import configure
    from rmmt.config import read_env
    settings=read_env(local_path(args.env))
    secrets=[v for k,v in settings.items() if any(word in k for word in ('SECRET','PASSWORD','APP_KEY','ACCOUNT','HTS','SALT','HASH'))]
    logger=configure(config,secrets)
    logger.write('system',f'START command={args.command or "service"} mode={config.mode} strategy={config.strategy} strategy_id={config.strategy_id}')
    for field,value in {'snapshot':config.snapshot_path,'daily':config.daily_path,'calendar':config.calendar_path,
                        'output':config.calendar_path,'quotes':config.quotes_path,'auto_collect':config.auto_collect}.items():
        if hasattr(args,field) and getattr(args,field) is None:setattr(args,field,value)
    command=args.command or 'service'
    if command=='check':
        checks={'configuration':config.public(),'config_hash':config.hash,'database_path':config.database_path(),'network_accessed':False,'orders_submitted':0,
            'credentials_configured':bool(credentials['KIS_'+config.environment.upper()+'_APP_KEY']),
            'root':str(ROOT)}
        try:config.validate_live();checks['order_mode_ready']=True
        except RMMTError as exc:checks['order_mode_ready']=False;checks['order_mode_reason']=str(exc)
        print_json(checks);return 0
    if command=='signal':
        snapshot=load_json(args.snapshot);signal=compute_signal(snapshot,config)
        output=export_report(config,signal=signal,snapshot=snapshot)
        print_json({'strategy':config.strategy,'strategy_id':config.strategy_id,'signal_date':signal['signal_date'],'execution_date':signal['execution_date'],'candidates':len(signal['candidates']),'selected':[r['symbol'] for r in signal['candidates'] if r['selected']],'report':str(output)});return 0
    if command in ('plan','paper'):
        snapshot=load_json(args.snapshot);quotes=load_json(args.quotes);at=timestamp(args.at)
        effective=replace(config,mode='PAPER') if command=='paper' else config
        signal=compute_signal(snapshot,effective)
        selected_db=args.db or effective.database_path()
        if command=='plan':
            positions={};cash=config.capital
            if local_path(selected_db).exists():
                ledger=Ledger(selected_db,effective,False,readonly=True)
                try:
                    ledger.db.execute('BEGIN');positions=ledger.positions();cash=ledger.cash;ledger.db.commit()
                finally:ledger.close()
            plan=plan_rebalance(signal,positions,cash,quotes,at,effective)
            output=export_report(effective,signal=signal,plan=plan,snapshot=snapshot)
            print_json({'plan':plan,'report':str(output),'orders_submitted':0});return 0
        with process_lock(str(local_path(selected_db))+'.lock'):
            ledger=Ledger(selected_db,effective)
            try:
                paper=PaperBroker(quotes);engine=Executor(effective,ledger,paper,quotes,clock=lambda:at)
                result=engine.run(signal,snapshot['execution_date'])
                output=export_report(effective,signal,result.get('plan'),ledger,snapshot,result)
                print_json({'state':result['state'],'report':str(output),'cash':str(ledger.cash),'holdings':len(ledger.positions()),'simulated':True})
                from rmmt.dashboard import Dashboard
                Dashboard(effective,args.snapshot,started_at=PROGRAM_STARTED_AT).show(ledger,quotes,state='PAPER: '+result['state'],force=True,now=at)
            finally:ledger.close()
        return 0
    if command=='calendar':
        broker=KIS(config,credentials);calendar=collect_calendar(broker,args.start,args.end)
        write_json(args.output,calendar);print_json({'sessions':len(calendar['sessions']),'file':str(local_path(args.output))});return 0
    if command=='set-capital':
        amount=Decimal(args.amount)
        if not amount.is_finite() or amount<=0 or amount%Decimal('10000')!=0:raise RMMTError('투자상한 --amount는 원 단위이며 10,000원의 양의 정수 배수여야 합니다.')
        path=local_path(db_path(args,config))
        import sqlite3
        with process_lock(str(path)+'.lock'):
            if not path.exists():raise RMMTError('기존 원장 없음')
            db=sqlite3.connect(path)
            try:
                old=db.execute("SELECT value FROM meta WHERE key='capital_limit'").fetchone()[0]
                temporary=replace(config,capital=Decimal(old))
            finally:db.close()
            ledger=Ledger(path,temporary,False)
            try:
                if ledger.orders(True):raise RMMTError('미완료 주문 중 투자상한 변경 금지')
                with ledger.db:
                    ledger.set_meta('capital_limit',str(amount))
                    ledger.db.execute('INSERT INTO audit(kind,payload,occurred_at) VALUES(?,?,?)',('CAPITAL_LIMIT_CHANGE',json.dumps({'old':old,'new':str(amount)}),datetime.now(timezone.utc).isoformat()))
            finally:ledger.close()
        print_json({'capital_limit':str(amount),'next_step':f'.env의 REVERSE_MOMENTUM_CAPITAL_MANWON={int(amount/10000)}으로 변경하세요. 현금/초기자산은 변하지 않았습니다.'});return 0
    path=db_path(args,config)
    if command=='rearm-empty':
        today=datetime.now(SEOUL).date().isoformat()
        if args.date!=today:raise RMMTError('재예약은 오늘 날짜에만 허용합니다.')
        try:
            if len(args.at)!=5:raise ValueError()
            scheduled=datetime.strptime(args.at,'%H:%M').time()
            if not datetime.strptime('09:00','%H:%M').time()<=scheduled<=datetime.strptime('15:00','%H:%M').time():raise ValueError()
        except ValueError:raise RMMTError('재예약 --at은 09:00~15:00 HH:MM(KST)이어야 합니다.') from None
        one_off=replace(config,execution_time=args.at)
        if datetime.now(SEOUL)>=execution_window(today,one_off)[0]:raise RMMTError('재예약 시각은 현재 시각보다 이후여야 합니다.')
        with process_lock(str(local_path(path))+'.lock'):
            ledger=Ledger(path,config,False)
            try:
                changed=ledger.rearm_empty_rebalance(today,args.reason,args.at)
                print_json({'day':today,'state':'REARMED','changed':changed,'scheduled_at':execution_window(today,one_off)[0].isoformat(),
                    'daily_default':config.execution_time,'orders_submitted':0})
            finally:ledger.close()
        return 0
    if command in ('status','dashboard'):
        ledger=Ledger(path,config,False,readonly=True)
        try:
            config=ledger.execution_config(config,datetime.now(SEOUL).date().isoformat());ledger.config=config
            if command=='status' and (args.json or not config.dashboard_enabled):print_json(ledger.summary())
            else:
                from rmmt.dashboard import Dashboard
                display=Dashboard(config,getattr(args,'snapshot',None),started_at=PROGRAM_STARTED_AT)
                while True:
                    quotes={}
                    quote_file=getattr(args,'quotes',None)
                    if quote_file and local_path(quote_file).exists():
                        try:quotes=load_json(quote_file)
                        except RMMTError:pass
                    ledger.db.execute('BEGIN')
                    try:display.show(ledger,quotes,state='로컬 원장 조회 / KIS 미접속',force=command=='status' or not args.watch)
                    finally:ledger.db.rollback()
                    if command!='dashboard' or not args.watch:break
                    time.sleep(config.dashboard_refresh_seconds)
        finally:ledger.close()
        return 0
    with process_lock(str(local_path(path))+'.lock'):
        base_config=config
        ledger=Ledger(path,config,create=command in ('init-ledger','service','run'))
        try:
            if command in ('run','service'):
                config=ledger.execution_config(base_config,datetime.now(SEOUL).date().isoformat());ledger.config=config
            if command in ('init-ledger','status'):
                print_json(ledger.summary());return 0
            if command=='cash-event':
                changed=ledger.cash_event(args.id,args.type,args.amount);print_json({'changed':changed,'validation':ledger.validate()});return 0
            if command=='reconcile-cost':
                order=next((o for o in ledger.orders() if o['intent_id']==args.intent),None)
                if not order or order['state'] not in ('FILLED','CANCELLED') or order['filled']<=0:raise RMMTError('확정 체결된 종료 주문만 실제 비용 조정 가능')
                ledger.apply_cumulative(args.intent,order['filled'],order['gross'],order['state'],args.fee,args.tax,config.mode=='PAPER')
                ledger.audit('ACTUAL_COST_RECONCILIATION',{'intent':args.intent,'fee':args.fee,'tax':args.tax})
                print_json({'validation':ledger.validate()});return 0
            if command=='corporate-action':
                changed=ledger.corporate_action(args.id,args.symbol,args.quantity,args.evidence);print_json({'changed':changed,'validation':ledger.validate()});return 0
            if command=='collect':
                source=KisMarketData(KIS(config,credentials),progress=print_json)
                result=collect_day(args.date,load_json(args.calendar),config,ledger.positions(),market_data=source)
                daily=load_json(config.daily_path)
                with ledger.db:ledger.mark_daily(args.date,daily['bars'],daily['sessions'])
                print_json(result);return 0
            if command in ('run','service'):
                from rmmt.dashboard import Dashboard
                display=Dashboard(config,args.snapshot,started_at=PROGRAM_STARTED_AT)
                display.show(ledger,state='실전 서비스 시작 / 주문 설정 점검',force=True)
                config.validate_live()
                if config.environment=='demo':raise RMMTError('DEMO 취소가능조회 미지원: 현재는 PAPER 전체 흐름으로 검증하세요.')
            broker=KIS(config,credentials);provider=LiveQuotes(broker,config);engine=Executor(config,ledger,broker,provider)
            if command=='broker-check':
                holdings,_=broker.balance();open_orders=broker.cancellable()
                print_json({'environment':config.environment,'broker_holding_count':len(holdings),'open_order_count':len(open_orders),
                    'own_quantity_covered':all(holdings.get(s,{}).get('quantity',0)>=p['quantity'] for s,p in ledger.positions().items()),
                    'full_shared_account_reconciliation':False,'orders_submitted':0});return 0
            if command=='bind-order':
                order=next((o for o in ledger.orders() if o['intent_id']==args.intent),None)
                if not order or order['state'] not in ('UNKNOWN','SUBMITTING'):raise RMMTError('귀속 확인 대상 UNKNOWN 주문 없음')
                rows=broker.daily_orders(ledger.run(order['run_id'])['day'])
                matches=[r for r in rows if str(r.get('odno'))==args.broker_id and str(r.get('ord_gno_brno'))==args.branch]
                if len(matches)!=1:raise RMMTError('증권사 주문 유일성 확인 실패')
                row=matches[0]
                if row['pdno']!=order['symbol'] or int(row['ord_qty'])!=order['quantity'] or row['sll_buy_dvsn_cd']!=('02' if order['side']=='BUY' else '01'):
                    raise RMMTError('귀속 주문의 종목/수량/방향 불일치')
                if order['state']=='SUBMITTING':ledger.transition(args.intent,'UNKNOWN')
                ledger.transition(args.intent,'ACKNOWLEDGED',args.broker_id,args.branch,reason='EXPLICIT_OPERATOR_BIND')
                engine.reconcile();print_json({'bound':args.intent,'validation':ledger.validate()});return 0
            if command=='reconcile':
                engine.reconcile()
                # Read-only broker query. No cancel or submit in this command.
                print_json(ledger.summary());return 0
            from rmmt.bootstrap import Preparation
            from rmmt.logs import readable_event
            screen_state={'value':'실행 준비'}
            def operational_result(value):
                print_json(value,console=not config.dashboard_enabled)
            def preparation_progress(value):
                stage_changed=display.update_workflow(value)
                screen_state['value']=readable_event(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False))
                operational_result(value)
                display.show(ledger,provider.cache,state=screen_state['value'],force=stage_changed)
            preparation_config=replace(config,calendar_path=args.calendar,daily_path=args.daily,snapshot_path=args.snapshot)
            preparation=Preparation(preparation_config,broker,provider.master,preparation_progress)
            def tick():
                nonlocal config
                now=datetime.now(SEOUL);today=now.date().isoformat()
                effective=ledger.execution_config(base_config,today)
                if effective.hash!=config.hash:
                    config=effective;ledger.config=config;engine.config=config;provider.config=config;display.config=config
                    display.signal_mtime=None
                    preparation.config=replace(config,calendar_path=args.calendar,daily_path=args.daily,snapshot_path=args.snapshot)
                # Repair accepted orders even when local calendar/data are missing.
                engine.reconcile()
                _,deadline=execution_window(today,config)
                if now>=deadline:engine.cancel_remaining()
                calendar=preparation.ensure_calendar(now)
                now=datetime.now(SEOUL);today=now.date().isoformat()
                if today not in calendar['sessions']:
                    # Cancellation recovery must not depend on a trading-day signal.
                    return {'state':'NON_SESSION','day':today}
                start,end=execution_window(today,config)
                prior=ledger.blocking_run(today)
                if prior:
                    if prior['state'] not in ('COMPLETE','WINDOW_CLOSED','INTERRUPTED','FAILED','SKIPPED'):
                        engine.cancel_remaining(prior['id']);ledger.finish_run(prior['id'],'INTERRUPTED')
                    return {'state':ledger.run(prior['id'])['state'],'day':today,'restarted':True}
                if datetime.now(SEOUL)>=end:
                    if preparation.skip_unready_cycle(ledger,calendar,datetime.now(SEOUL)):
                        return {'state':'SKIPPED','day':today,'reason':'DATA_NOT_READY_BEFORE_DEADLINE'}
                    return {'state':'OUTSIDE_WINDOW','day':today}
                if datetime.now(SEOUL)<start:
                    prepared=preparation.prepare_archive(ledger,calendar,datetime.now(SEOUL))
                    return {'state':'WAITING_WINDOW','day':today,'archive_ready':prepared}
                data_error=None;signal=None;snapshot=None
                try:
                    if preparation.prepare_rebalance(ledger,calendar,now):
                        apply_daily(ledger,args.daily,calendar,today)
                        signal,snapshot=ready_signal(args.snapshot,calendar,today,config)
                except RMMTError as exc:
                    data_error=str(exc)
                    ledger.audit('SIGNAL_OR_VALUATION_NOT_READY',{'day':today,'reason':data_error})
                    # Own-position monitoring remains usable even if Yahoo selection inputs fail.
                    try:apply_daily(ledger,args.daily,calendar,today)
                    except RMMTError:pass
                if datetime.now(SEOUL)>=end:
                    preparation.skip_unready_cycle(ledger,calendar,datetime.now(SEOUL))
                    return {'state':'SKIPPED','day':today,'reason':'DATA_NOT_READY_BEFORE_DEADLINE'}
                if data_error and not any(p.get('exit_required') for p in ledger.positions().values()):
                    return {'state':'WAITING_DATA','day':today,'error':data_error,'retry_after':config.error_retry_seconds}
                if signal is None and not any(p.get('exit_required') for p in ledger.positions().values()):
                    return {'state':'WAITING_SELECTION','day':today}
                screen_state['value']='회복매도 실행 중' if signal is None else '리밸런싱 실행 중'
                result=engine.run(signal,today,recovery_only=signal is None)
                if data_error:result['new_rebalance_blocked_reason']=data_error
                if result.get('restarted'):
                    return {'state':result['state'],'day':today,'restarted':True}
                output=export_report(config,signal,result.get('plan'),ledger,snapshot,result)
                return {'state':result['state'],'day':today,'report':str(output)}
            def refresh_screen():display.show(ledger,provider.cache,state=screen_state['value'])
            def refresh_prices():
                if not config.dashboard_enabled:return
                symbols=set(ledger.positions())
                symbols.update(r['symbol'] for r in (display.cached_signal or {}).get('candidates',[]) if r['selected'])
                symbols.update(preview_holdings(config))
                failed=provider.refresh_display(symbols)
                if failed:
                    from rmmt.logs import event
                    event('현재가 조회 실패: '+', '.join(failed)+' — 이전 평가가격 유지')
            engine.observer=refresh_screen
            engine.progress=preparation_progress
            display.show(ledger,provider.cache,state='실행 준비',force=True)
            if command=='run':
                result=tick();operational_result(result)
                display.show(ledger,provider.cache,state=readable_event(json.dumps(result,ensure_ascii=False,default=str)),force=True)
                return 0
            last_state=None;recovery_attempt=0
            while True:
                try:
                    result=tick();recovery_attempt=0
                    screen_state['value']=readable_event(json.dumps(result,ensure_ascii=False,default=str))
                    key=(result['state'],result['day'],result.get('error'))
                    if key!=last_state:
                        operational_result(result);last_state=key
                        display.show(ledger,provider.cache,state=screen_state['value'],force=True)
                except RMMTError as exc:
                    screen_state['value']='복구 필요: '+str(exc)
                    refresh_screen()
                    key=('RECOVERY_REQUIRED',str(exc),datetime.now(SEOUL).date().isoformat())
                    recovery_attempt+=1
                    operational_result({'state':'RECOVERY_REQUIRED','error':str(exc),'retry_attempt':recovery_attempt,
                                        'time':datetime.now(SEOUL).isoformat()});last_state=key
                    result={'state':'RECOVERY_REQUIRED','day':datetime.now(SEOUL).date().isoformat()}
                refresh_prices()
                refresh_screen()
                poll_now=datetime.now(SEOUL)
                poll_start,poll_end=execution_window(poll_now.date().isoformat(),config)
                delay=config.poll_seconds if poll_start<=poll_now<poll_end else config.idle_seconds
                if result['state'] in ('WAITING_DATA','RECOVERY_REQUIRED'):delay=config.error_retry_seconds
                remaining=delay
                while remaining>0:
                    pause=min(remaining,config.dashboard_refresh_seconds) if config.dashboard_enabled else remaining
                    time.sleep(pause);remaining-=pause
                    refresh_prices()
                    refresh_screen()
        finally:ledger.close()

def logged_main(argv=None):
    from rmmt.logs import current
    import traceback
    try:return main(argv)
    except KeyboardInterrupt:
        logger=current()
        if logger:logger.write('system','사용자 요청으로 실행 중단 (Ctrl+C)')
        raise
    except Exception:
        logger=current()
        if logger:
            logger.write('system',traceback.format_exc())
            logger.write('screen','RMMT 실행 중단: system 로그를 확인하세요.')
        raise
    finally:
        logger=current()
        if logger:logger.write('system','STOP');logger.close()

if __name__=='__main__':
    try:raise SystemExit(logged_main())
    except KeyboardInterrupt:
        print('중단했습니다. 미완료 주문은 다음 reconcile/run에서 반드시 확인하세요.',file=sys.stderr);raise SystemExit(130)
    except (RMMTError,ValueError,KeyError,TypeError) as exc:
        print('RMMT 중단: '+(str(exc) if isinstance(exc,RMMTError) else '입력/API 데이터 형식 오류'),file=sys.stderr);raise SystemExit(2)
