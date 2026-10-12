"""Sell-before-buy orchestration and broker reconciliation; owns only RMMT orders."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import time
from .config import RMMTError
from .strategy import decimal,timestamp,quote_reasons,plan_rebalance,execution_window
from .kis import BrokerRejected,BrokerUnknown

def log_filter_block(symbol, side, reasons):
    from .logs import event
    labels={'NOT_TRADABLE':'거래 불가·거래정지',
        'QUOTE_STALE_OR_INVALID':'현재가 만료 또는 유효하지 않음','QUOTE_UNKNOWN':'현재가 확인 불가',
        'NO_CURRENT_TRADE_OBSERVATION':'당일 거래 확인 불가','REGULATORY_STATUS_UNKNOWN':'경고 정보 확인 불가 또는 만료',
        'REGULATORY_MANAGED':'관리종목','REGULATORY_CAUTIONARY':'투자주의환기종목',
        'REGULATORY_CAUTION':'투자주의종목','REGULATORY_WARNING':'투자경고종목',
        'REGULATORY_RISK':'투자위험종목','EXIT_REQUIRED':'거래 재개 후 청산 대상'}
    event(f"{symbol} {'매수' if side=='BUY' else '매도'} 차단: "+' / '.join(labels.get(reason,reason) for reason in reasons))

class PaperBroker:
    """Deterministic fill emulator. NEVER a statement about actual KIS executions."""
    def __init__(self,quotes):self.quotes=quotes;self.orders={};self.sequence=0
    def quote(self,symbol):return self.quotes[symbol]
    def submit(self,order):
        self.sequence+=1;key='PAPER'+str(self.sequence)
        self.orders[key]=dict(order);return key,'SIMULATED'

class Executor:
    def __init__(self,config,ledger,broker,quotes,clock=None,sleep=None):
        self.config=config;self.ledger=ledger;self.broker=broker;self.quotes=quotes
        self.clock=clock or (lambda:datetime.now(timezone.utc));self.sleep=sleep or time.sleep;self.observer=None;self.progress=None
        self.active_phase=None;self.phase_completed=0;self.phase_total=0;self.phase_blocked=0;self.run_day=None;self.progress_run=None
    def phase_progress(self,status='RUNNING',symbol=None,detail=None):
        if not self.progress or not self.active_phase:return
        side=self.active_phase
        orders=[o for o in self.ledger.orders(run_id=self.progress_run) if o['side']==side] if self.progress_run else []
        filled=sum(o['filled'] for o in orders);quantity=sum(o['quantity'] for o in orders)
        rejected=sum(o['state']=='REJECTED' for o in orders)
        cancelled=sum(o['state']=='CANCELLED' for o in orders)
        if status=='COMPLETE' and (rejected or cancelled or self.phase_blocked):status='PARTIAL'
        value={'stage':'PIPELINE_PROGRESS','phase':side,'status':status,'day':self.run_day,
            'completed':self.phase_completed,'total':self.phase_total,
            'detail':detail or f'체결 {filled}/{quantity}주 / 거절 {rejected} / 취소 {cancelled} / 미주문 {self.phase_blocked}'}
        if symbol:value['symbol']=symbol
        try:self.progress(value)
        except Exception:
            from .logs import event
            event('DASHBOARD_ERROR: 단계 진행상황 출력 실패; 주문 원장 보존')
    def start_phase(self,phase,total,detail=None):
        self.active_phase=phase;self.phase_completed=0;self.phase_total=total;self.phase_blocked=0
        self.phase_progress(detail=detail or ('대상 없음' if not total else None))
    def observe(self):
        if self.observer:
            try:self.observer()
            except Exception:
                from .logs import event
                event('DASHBOARD_ERROR: observer failed; order flow unchanged')
    def fresh(self,symbol):
        return self.quotes.get(symbol) if hasattr(self.quotes,'get') else None
    def reconcile(self):
        if isinstance(self.broker,PaperBroker):return
        active=self.ledger.orders(True)
        days=sorted({self.ledger.run(o['run_id'])['day'] for o in active})
        for day in days:
            rows=self.broker.daily_orders(day)
            for o in [v for v in active if self.ledger.run(v['run_id'])['day']==day]:
                if o['state']=='PLANNED':continue
                if o['state']=='SUBMITTING':
                    self.ledger.transition(o['intent_id'],'UNKNOWN',reason='PROCESS_RESTART_DURING_SUBMIT')
                    o['state']='UNKNOWN'
                if not o['broker_id']:
                    # Shared-account similarity is not proof of strategy ownership.
                    continue
                matches=[r for r in rows if str(r.get('odno'))==o['broker_id'] and str(r.get('ord_gno_brno'))==o['branch']]
                if len(matches)!=1:continue
                row=matches[0]
                if row.get('pdno')!=o['symbol'] or row.get('sll_buy_dvsn_cd')!=('02' if o['side']=='BUY' else '01') or int(row['ord_qty'])!=o['quantity']:
                    raise RMMTError('브로커 주문과 원장 의도 불일치')
                filled=int(row['tot_ccld_qty']);gross=decimal(row['tot_ccld_amt'])
                cancelled=int(row.get('cnc_cfrm_qty','0') or 0);rejected=int(row.get('rjct_qty','0') or 0)
                terminal=None
                if filled==o['quantity']:terminal='FILLED'
                elif rejected==o['quantity'] and not filled:terminal='REJECTED'
                elif int(row.get('rmn_qty','-1'))==0 and filled+cancelled==o['quantity']:terminal='CANCELLED'
                self.ledger.apply_cumulative(o['intent_id'],filled,gross,terminal)
        if not self.ledger.validate()['passed']:raise RMMTError('원장 불변조건 실패; 신규 주문 중단')
    def cancel_remaining(self,run_id=None):
        for o in self.ledger.orders(True,run_id):
            if o['state']=='PLANNED':
                self.ledger.transition(o['intent_id'],'CANCELLED',reason='UNSUBMITTED_WINDOW_END');continue
            if not o['broker_id'] or o['state']=='UNKNOWN':continue
            if isinstance(self.broker,PaperBroker):
                self.ledger.apply_cumulative(o['intent_id'],o['filled'],o['gross'],'CANCELLED',simulated=True);continue
            if o['state']=='CANCEL_PENDING':continue
            self.ledger.transition(o['intent_id'],'CANCEL_PENDING')
            try:self.broker.cancel(o)
            except BrokerUnknown:
                self.ledger.transition(o['intent_id'],'UNKNOWN',reason='CANCEL_RESPONSE_UNKNOWN')
            except (BrokerRejected,RMMTError):
                # Keep reservations until order query proves cancellation or fill.
                self.ledger.audit('CANCEL_NOT_CONFIRMED',{'intent':o['intent_id']})
        self.reconcile()
    def assert_account(self):
        if isinstance(self.broker,PaperBroker):return
        holdings,_=self.broker.balance()
        for symbol,p in self.ledger.positions().items():
            if holdings.get(symbol,{}).get('quantity',0)<p['quantity']:
                raise RMMTError('RMMT 귀속수량이 실제 잔고보다 큽니다: '+symbol)
        known={(o['broker_id'],o['branch']) for o in self.ledger.orders() if o['broker_id']}
        foreign=[r for r in self.broker.cancellable() if int(r.get('psbl_qty','0'))>0 and (str(r.get('odno')),str(r.get('ord_gno_brno'))) not in known]
        if foreign:raise RMMTError('다른 전략/수동 미체결 주문 존재: 시간 분리 대조 후 실행 필요')
        if self.ledger.orders(True):raise RMMTError('이전 미완료/UNKNOWN 주문이 있습니다. 복구 후 신규 실행하세요.')
    def wait_order(self,intent,end):
        if isinstance(self.broker,PaperBroker):return
        while self.clock()<end:
            self.reconcile()
            self.phase_progress(symbol=self._order(intent)['symbol'])
            self.observe()
            order=next(o for o in self.ledger.orders() if o['intent_id']==intent)
            if order['state'] in ('FILLED','CANCELLED','REJECTED'):return
            if order['state']=='UNKNOWN':raise RMMTError('접수 여부 UNKNOWN: 자동 재주문/대체 금지')
            self.sleep(min(self.config.poll_seconds,max(0,(end-self.clock()).total_seconds())))
    def submit(self,run_id,request,end,sequence=0):
        if self.clock()>=end:return None
        symbol=request['symbol'];side=request['side'];quote=self.fresh(symbol)
        reasons=quote_reasons(quote,self.clock(),self.config,buy=side=='BUY')
        if reasons:
            self.ledger.audit('PREORDER_FILTER_BLOCK',{'symbol':symbol,'side':side,'reasons':reasons})
            log_filter_block(symbol,side,reasons);self.observe();return None
        qty=request['quantity'];price=decimal(quote['price'])
        bound=price if self.config.order_policy=='LIMIT' or side=='SELL' else decimal(quote['upper_limit'])
        if bound<=0:raise RMMTError('주문 가격/상한가 경계 오류')
        if self.config.order_policy=='LIMIT':
            tick=int(quote.get('tick',0))
            if tick<=0 or int(price)%tick:raise RMMTError('현재가 지정가 호가단위 검증 실패')
        if side=='BUY':
            available=self.ledger.available_cash()
            if not isinstance(self.broker,PaperBroker):
                broker_cash,broker_qty=self.broker.buy_power(symbol)
                available=min(available,broker_cash);qty=min(qty,broker_qty)
            # Market orders reserve worst permitted price, not an invented slippage percentage.
            invested=Decimal(0)
            for held,p in self.ledger.positions().items():
                held_quote=self.fresh(held)
                mark=Decimal(0) if p['exit_required'] else decimal(held_quote['price']) if not quote_reasons(held_quote,self.clock(),self.config,False) else decimal(p['mark'])
                invested+=p['quantity']*mark
            available=min(available,max(Decimal(0),self.config.capital-invested-self.ledger.reserved_cash()))
            qty=min(qty,int((available/(bound*(1+self.config.buy_fee))).to_integral_value(rounding=ROUND_FLOOR)))
        else:
            qty=min(qty,self.ledger.available_quantity(symbol))
            if not isinstance(self.broker,PaperBroker):
                account,_=self.broker.balance();qty=min(qty,account.get(symbol,{}).get('sellable',0))
        if qty<=0:return None
        # All slow prechecks finished; recheck time/status just before intent submission.
        quote=self.fresh(symbol)
        if self.clock()>=end or quote_reasons(quote,self.clock(),self.config,buy=side=='BUY'):return None
        if decimal(quote['price'])!=price:
            # Do not chase a precomputed limit; new quote is a later planning opportunity.
            if self.config.order_policy=='LIMIT':return None
        if side=='BUY' and self.config.order_policy=='MARKET' and decimal(quote['upper_limit'])>bound:return None
        order,created=self.ledger.create_order(run_id,symbol,side,qty,bound,sequence)
        if not created:
            # Never resubmit a persisted intent, even if its last response was lost.
            return order
        self.ledger.transition(order['intent_id'],'SUBMITTING')
        try:
            broker_id,branch=self.broker.submit(order)
            self.ledger.transition(order['intent_id'],'ACKNOWLEDGED',broker_id,branch)
        except BrokerRejected as exc:
            self.ledger.transition(order['intent_id'],'REJECTED',reason=str(exc));return self._order(order['intent_id'])
        except Exception:
            self.ledger.transition(order['intent_id'],'UNKNOWN',reason='SUBMIT_RESPONSE_UNKNOWN')
            raise RMMTError('주문 접수 여부 UNKNOWN: 복구 전 재주문하지 않습니다.') from None
        self.observe()
        self.phase_progress(symbol=symbol)
        if isinstance(self.broker,PaperBroker):
            self.ledger.apply_cumulative(order['intent_id'],qty,qty*price,'FILLED',simulated=True)
        else:self.wait_order(order['intent_id'],end)
        return self._order(order['intent_id'])
    def _order(self,intent):return next(o for o in self.ledger.orders() if o['intent_id']==intent)
    def run(self,signal,day,recovery_only=False):
        start,end=execution_window(day,self.config);at=self.clock()
        self.reconcile()
        # Previous-day obligations survive a closed run and must still be cancelled/reconciled.
        old=[o for o in self.ledger.orders(True) if self.ledger.run(o['run_id'])['day']<day]
        for run in {o['run_id'] for o in old}:self.cancel_remaining(run)
        if at>=end:
            for run in {o['run_id'] for o in self.ledger.orders(True)}:self.cancel_remaining(run)
            return {'state':'OUTSIDE_WINDOW','day':day}
        if at<start:return {'state':'WAITING_WINDOW','day':day}
        if not recovery_only and (not signal or signal['execution_date']!=day):
            recovery_only=True
        kind='RECOVERY' if recovery_only else 'REBALANCE'
        other=self.ledger.blocking_run(day,exclude_kind=kind)
        if other:
            return {'state':other['state'],'day':day,'restarted':True,'reason':'DAY_ALREADY_EXECUTED'}
        run_id=f'{self.config.strategy_id}:{day}:{kind}'
        self.run_day=day;self.progress_run=run_id
        prior=self.ledger.run(run_id)
        if prior and prior['state']!='REARMED':
            # Restart reconciles/cancels previously submitted orders, but never runs the day twice.
            if prior['state'] in ('COMPLETE','WINDOW_CLOSED','INTERRUPTED','FAILED'):
                return {'state':prior['state'],'day':day,'restarted':True}
            self.cancel_remaining(run_id);self.ledger.finish_run(run_id,'INTERRUPTED')
            return {'state':'INTERRUPTED','day':day,'restarted':True}
        self.start_phase('SELL',0,detail='주문 전 계좌 검증·계획 계산')
        try:self.assert_account()
        except RMMTError as exc:
            self.phase_progress('ERROR',detail='주문 전 계좌 검증 실패: '+str(exc))
            raise
        _,created=self.ledger.begin_run(run_id,day,kind,{'signal_id':signal.get('signal_id') if signal else None,'selection_positions':self.ledger.positions()})
        try:
            positions=self.ledger.positions();quotes={};selected=[];checks=[]
            if not recovery_only:
                for candidate in signal['candidates']:
                    symbol=candidate['symbol'];quote=self.fresh(symbol);quotes[symbol]=quote
                    reasons=quote_reasons(quote,self.clock(),self.config)
                    if positions.get(symbol,{}).get('exit_required'):reasons.append('EXIT_REQUIRED')
                    checks.append({'symbol':symbol,'reasons':reasons})
                    if reasons:log_filter_block(symbol,'BUY',reasons)
                    if not reasons:selected.append(symbol)
                    if len(selected)>=self.config.holdings:break
            for symbol in set(positions)|set(selected):quotes[symbol]=self.fresh(symbol)
            for symbol,p in positions.items():
                if not p['exit_required'] and not quote_reasons(quotes[symbol],self.clock(),self.config,False):
                    p['mark']=quotes[symbol]['price']
            if recovery_only:
                sells=[{'symbol':s,'side':'SELL','quantity':p['quantity']} for s,p in positions.items()
                       if p['exit_required'] and quotes[s].get('traded_today') and not quote_reasons(quotes[s],self.clock(),self.config,False)]
                plan={'selected':[],'sells':sells,'buys':[],'checks':[]}
            else:
                filtered_signal=dict(signal,candidates=[c for c in signal['candidates'] if c['symbol'] in selected])
                plan=plan_rebalance(filtered_signal,positions,self.ledger.cash,quotes,self.clock(),self.config)
                plan['checks']=checks
                # A zero-valued holding is sold only after actual resumed trading observation.
                plan['sells']=[o for o in plan['sells'] if not positions.get(o['symbol'],{}).get('exit_required') or quotes[o['symbol']].get('traded_today')]
            self.ledger.audit('INITIAL_PLAN',dict(plan,run_id=run_id))
            self.start_phase('SELL',len(plan['sells']))
            for request in plan['sells']:
                result=self.submit(run_id,request,end)
                if result is None:self.phase_blocked+=1
                self.phase_completed+=1;self.phase_progress(symbol=request['symbol'])
                if self.clock()>=end:break
            sell_active=[o for o in self.ledger.orders(True,run_id) if o['side']=='SELL']
            if sell_active or self.clock()>=end:
                self.cancel_remaining(run_id);self.ledger.finish_run(run_id,'WINDOW_CLOSED')
                self.phase_progress('TIMEOUT',detail='매도 미완료 또는 주문창 종료 — 매수 차단')
                self.start_phase('BUY',0);self.phase_progress('SKIPPED',detail='매도 단계 미완료')
                return {'state':'WINDOW_CLOSED','day':day,'plan':plan}
            self.phase_progress('COMPLETE',detail='매도 대상 없음' if not plan['sells'] else None)
            if not recovery_only:
                # Recompute equity and integer targets from confirmed sale fills.
                current=self.ledger.positions()
                for symbol in set(current)|set(plan['selected']):quotes[symbol]=self.fresh(symbol)
                for symbol,p in current.items():
                    if not p['exit_required'] and not quote_reasons(quotes[symbol],self.clock(),self.config,False):p['mark']=quotes[symbol]['price']
                selected_signal=dict(signal,candidates=[c for c in signal['candidates'] if c['symbol'] in plan['selected']])
                buy_plan=plan_rebalance(selected_signal,current,self.ledger.cash,quotes,self.clock(),self.config)
                # Scale every buy shortage by the same factor if worst-price reservations do not fit.
                needed=Decimal(0)
                for request in buy_plan['buys']:
                    q=quotes[request['symbol']]
                    bound=decimal(q['upper_limit']) if self.config.order_policy=='MARKET' else decimal(q['price'])
                    needed+=request['quantity']*bound*(1+self.config.buy_fee)
                held_value=sum((p['quantity']*decimal(p['mark']) for p in current.values()),Decimal(0))
                budget=min(self.ledger.available_cash(),max(Decimal(0),self.config.capital-held_value))
                scale=min(Decimal(1),budget/needed) if needed else Decimal(1)
                if scale<1:
                    for request in buy_plan['buys']:
                        request['quantity']=int((request['quantity']*scale).to_integral_value(rounding=ROUND_FLOOR))
                        request['target_value']=str(decimal(request['target_value'])*scale)
                    buy_plan['buys']=[r for r in buy_plan['buys'] if r['quantity']>0]
                buy_plan['cash_scale']=str(scale)
                self.ledger.audit('POST_SELL_PLAN',dict(buy_plan,run_id=run_id))
                self.start_phase('BUY',len(buy_plan['buys']))
                attempted=set(plan['selected']);slots=list(buy_plan['selected']);sequence=0
                for request in buy_plan['buys']:
                    if self.clock()>=end:break
                    result=self.submit(run_id,request,end)
                    if result is None:self.phase_blocked+=1
                    self.phase_completed+=1;self.phase_progress(symbol=request['symbol'])
                    if result and result['filled']>0:continue
                    if result and result['state'] not in ('REJECTED','CANCELLED'):continue
                    # Keep a partially-filled/previously-owned symbol in its slot; no extra replacement.
                    if self.ledger.positions().get(request['symbol'],{}).get('quantity',0)>0:continue
                    for candidate in signal['candidates']:
                        replacement=candidate['symbol']
                        if replacement in attempted or self.ledger.positions().get(replacement,{}).get('exit_required'):continue
                        attempted.add(replacement);quote=self.fresh(replacement)
                        if quote_reasons(quote,self.clock(),self.config):continue
                        amount=decimal(request['target_value']);qty=int((amount/(decimal(quote['price'])*(1+self.config.buy_fee))).to_integral_value(rounding=ROUND_FLOOR))
                        if qty<=0:continue
                        sequence+=1
                        self.ledger.audit('REPLACEMENT',{'old':request['symbol'],'new':replacement,'sequence':sequence,'signal_id':signal['signal_id'],'run_id':run_id})
                        substitute={'symbol':replacement,'side':'BUY','quantity':qty,'target_value':str(amount)}
                        result=self.submit(run_id,substitute,end,sequence)
                        self.phase_progress(symbol=replacement)
                        if result and (result['filled']>0 or result['state'] not in ('REJECTED','CANCELLED')):break
                        if self.clock()>=end:break
            else:
                self.start_phase('BUY',0)
            self.cancel_remaining(run_id)
            # An exhausted window cannot mean COMPLETE when buy requests remain.
            timed_out=self.clock()>=end or self.phase_completed<self.phase_total
            state='WINDOW_CLOSED' if timed_out or self.ledger.orders(True,run_id) else 'COMPLETE'
            self.ledger.finish_run(run_id,state)
            with self.ledger.db:self.ledger.record_nav(day)
            if not self.ledger.validate()['passed']:raise RMMTError('체결 후 원장 검증 실패')
            self.phase_progress('TIMEOUT' if state=='WINDOW_CLOSED' or self.phase_completed<self.phase_total else 'COMPLETE',
                detail='회복매도 회차 — 매수 없음' if recovery_only else '매수 대상 없음' if not self.phase_total else None)
            return {'state':state,'day':day,'plan':plan,'ledger':self.ledger.summary()}
        except Exception as exc:
            self.phase_progress('ERROR',detail=str(exc))
            self.ledger.finish_run(run_id,'FAILED')
            # Preserve the original exception. Reservations are kept if cancellation cannot be confirmed.
            try:self.cancel_remaining(run_id)
            except Exception:self.ledger.audit('RECOVERY_REQUIRED',{'run_id':run_id})
            raise

