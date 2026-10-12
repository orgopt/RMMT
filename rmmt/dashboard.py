"""Read-only Korean terminal dashboard. No broker calls or ledger mutation."""
from datetime import datetime, date, time as day_time
from decimal import Decimal
import json
from statistics import median
import sys
import time
import unicodedata
from .config import local_path
from .strategy import SEOUL, FLAGS, timestamp, execution_window, rebalance_dates

LABELS={'MANAGED':'관리','CAUTIONARY':'주의환기','CAUTION':'투자주의','WARNING':'투자경고','RISK':'투자위험'}
PHASE_LABELS={'COLLECTION':'수집','VALIDATION':'검증','SELECTION':'선정','SELL':'매도','BUY':'매수'}
PHASE_STATUS={'WAITING':'대기','RUNNING':'진행 중','COMPLETE':'완료','ERROR':'오류',
              'TIMEOUT':'시간 초과','PARTIAL':'일부 미체결·미주문','SKIPPED':'미실행'}

def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))

def width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in value)

def clip_width(value,limit):
    value=clean(value)
    if width(value)<=limit:return value
    result=''
    for char in value:
        if width(result+char)>limit-width('…'):break
        result+=char
    return result+'…'

def table(headers, rows, totals=None):
    numeric={'보유수량','수량','평단','매수금액','현재금액','평가금액','매도금액','평가손익(%)'}
    rows=[[clean(v) for v in row] for row in [headers]+rows]
    if '종목명' in headers:
        index=headers.index('종목명')
        for row in rows[1:]:
            row[index]=clip_width(row[index],20)
    footer=[clean(v) for v in totals] if totals is not None else None
    measured=rows+([footer] if footer is not None else [])
    sizes=[20 if headers[i]=='종목명' else max(width(row[i]) for row in measured) for i in range(len(headers))]
    def line(row,heading=False):
        return '  '.join((' '*(sizes[i]-width(v))+v) if headers[i] not in ('종목명','상태') and (heading or headers[i] in numeric)
            else (v+' '*(sizes[i]-width(v))) for i,v in enumerate(row))
    divider='--'.join('-'*n for n in sizes)
    return '\n'.join([line(rows[0],True),divider]+[line(r) for r in rows[1:]]+([divider,line(footer)] if footer is not None else []))

def money(value):return f'{Decimal(str(value)):,.0f}'

def selection_summary(signal):
    if signal is None:return '종목선정: 선정 전'
    values=[Decimal(row['formation_return'])*100 for row in signal['candidates'] if row['selected']]
    if not values:return '종목선정: 0종목 선정완료(적격 종목 없음)'
    average=sum(values,Decimal(0))/len(values)
    return f'종목선정: {len(values)}종목 선정완료(평균 {average:+.2f}% / 중위 {median(values):+.2f}%)'

def latest_realized_trade(ledger):
    """Replay own fills with the ledger's average-cost rule, including corrections."""
    events=[(row['occurred_at'],0,row['rowid'],dict(row)) for row in
        ledger.db.execute('SELECT rowid,* FROM fills ORDER BY rowid')]
    events.extend((row['occurred_at'],1,row['rowid'],dict(row)) for row in
        ledger.db.execute("SELECT rowid,* FROM quantity_events WHERE type='CORPORATE_ACTION'"))
    positions={};sales={};latest=None
    for _,kind,_,row in sorted(events):
        qty,cost=positions.get(row['symbol'],(0,Decimal(0)))
        if kind:
            positions[row['symbol']]=(qty+row['delta'],cost);continue
        gross,fee,tax=(Decimal(row[key]) for key in ('gross','fee','tax'))
        if row['side']=='BUY':positions[row['symbol']]=(qty+row['qty'],cost+gross+fee)
        else:
            if row['qty']>qty or (row['qty'] and qty<=0):return None
            remaining=cost*Decimal(qty-row['qty'])/qty if qty else Decimal(0)
            removed=cost-remaining
            positions[row['symbol']]=(qty-row['qty'],remaining)
            item=sales.setdefault(row['intent_id'],{'profit':Decimal(0),'cost':Decimal(0),'at':None})
            item['profit']+=gross-fee-tax-removed;item['cost']+=removed
            if row['qty']:
                item['at']=row['occurred_at'];latest=row['intent_id']
    return sales.get(latest)

class Dashboard:
    def __init__(self,config,snapshot_path=None,stream=None,started_at=None):
        self.config=config;self.snapshot_path=snapshot_path or config.snapshot_path
        self.stream=stream or sys.stdout;self.started=(started_at or datetime.now(SEOUL)).astimezone(SEOUL)
        self.last_refresh=None;self.last_logged=None;self.cached_snapshot=None;self.snapshot_mtime=None;self.cached_signal=None;self.signal_mtime=None
        self.trade_signature=None;self.cached_trade=None
        self.workflow={};self.workflow_day=None

    def update_workflow(self,event):
        if not isinstance(event,dict) or event.get('stage')!='PIPELINE_PROGRESS':return False
        phase=event.get('phase')
        if phase not in PHASE_LABELS:return False
        day=event.get('day',self.workflow_day)
        if day!=self.workflow_day or (phase=='COLLECTION' and event.get('status')=='RUNNING'
            and (self.workflow.get(phase,{}).get('status') in ('ERROR','TIMEOUT')
                 or event.get('detail','').startswith('직전 거래일 '))):
            self.workflow={name:{'status':'WAITING'} for name in PHASE_LABELS};self.workflow_day=day
        before=self.workflow.get(phase,{})
        changed=before.get('status')!=event.get('status')
        self.workflow[phase]=dict(before,**event)
        if event.get('status')=='COMPLETE' and 'total' in self.workflow[phase]:
            self.workflow[phase]['completed']=self.workflow[phase]['total']
        return changed

    def workflow_lines(self):
        if not self.workflow:return []
        lines=['단계별 진행상황'+(' ('+self.workflow_day+' KST)' if self.workflow_day else '')]
        blocked=False
        for name,label in PHASE_LABELS.items():
            item=self.workflow.get(name,{'status':'WAITING'})
            details=[]
            if 'total' in item:
                total=item['total'];done=item.get('completed',0)
                details.append(f'{done}/{total}'+(f' ({100*done/total:.1f}%)' if total else ''))
            for key in ('server','symbol','detail','error'):
                if item.get(key):details.append(str(item[key]))
            if item.get('retry_after') and item.get('status')=='ERROR':details.append(f'{item["retry_after"]:g}초 후 재조회')
            status='SKIPPED' if blocked and item['status']=='WAITING' else item['status']
            lines.append(label+': '+PHASE_STATUS.get(status,status)
                +(' | '+clip_width(' | '.join(details),180) if details else ''))
            if item['status'] in ('ERROR','TIMEOUT'):blocked=True
        return lines

    def snapshot(self):
        path=local_path(self.snapshot_path)
        try:
            mtime=path.stat().st_mtime_ns
            if mtime!=self.snapshot_mtime:
                self.cached_snapshot=json.loads(path.read_text(encoding='utf-8'));self.snapshot_mtime=mtime
            return self.cached_snapshot,None
        except (OSError,ValueError):return None,'선정 자료 없음 또는 읽기 실패'

    def schedule(self,ledger,now,positions):
        """Expected strategy times from the verified calendar; never submit orders."""
        now=now.astimezone(SEOUL);today=now.date().isoformat()
        try:
            calendar=json.loads(local_path(self.config.calendar_path).read_text(encoding='utf-8'))
            sessions=calendar['sessions']
            if (calendar.get('source')!='KIS chk-holiday' or calendar.get('calendar_complete') is not True
                or not calendar['start']<=today<calendar['end'] or not isinstance(sessions,list)
                or sessions!=sorted(set(sessions))):raise ValueError('calendar')
            days=[date.fromisoformat(day) for day in sessions]
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            return ['다음일정: 거래일 달력 준비 후 확인 (KST)']
        try:upcoming=rebalance_dates(calendar,self.config)
        except Exception:return ['다음일정: 리밸런싱 기준일 포함 달력 준비 후 확인 (KST)']
        events=[]
        try:selection_snapshot=json.loads(local_path(self.snapshot_path).read_text(encoding='utf-8'))
        except (OSError,ValueError):selection_snapshot={}
        if not isinstance(selection_snapshot,dict):selection_snapshot={}
        for day in upcoming:
            start,end=execution_window(day,self.config)
            if now>=end or ledger.blocking_run(day):continue
            done=(selection_snapshot.get('selection_date')==day
                and selection_snapshot.get('selection_config_hash')==self.config.hash)
            stamp=start.strftime('%Y.%m.%d %H:%M')+' KST'
            if not done:
                if self.config.history_bundle_path:
                    morning=datetime.combine(date.fromisoformat(day),day_time.fromisoformat(self.config.collection_time),SEOUL)
                    events.append('증분수집('+morning.strftime('%Y.%m.%d %H:%M')+' KST) / 종목선정('+stamp+')')
                else:events.append('수집·종목선정('+stamp+')')
            events.append('리밸런싱('+stamp+' 선정 완료 직후)')
            if len(events)>=2:break
        lines=['다음일정: '+(' '.join(events[:2]) if events else '달력 범위 내 일정 없음')]
        def stamp(at):return at.strftime('%Y.%m.%d %H:%M')+' KST'
        if any(p.get('exit_required') for p in positions.values()):
            recovery=next((execution_window(day,self.config)[0] for day in sessions
                if now<execution_window(day,self.config)[1]
                and not ledger.db.execute("SELECT 1 FROM runs WHERE day=? AND kind IN ('RECOVERY','REBALANCE')",(day,)).fetchone()),None)
            lines.append('회복매도: '+(stamp(recovery)+' (거래 재개 시)' if recovery else '달력 범위 내 일정 없음'))
        return lines

    def render(self,ledger,quotes=None,state='상태 조회',now=None,recent=()):
        now=now or datetime.now(SEOUL);quotes=quotes or {}
        snapshot,error=self.snapshot();snapshot=snapshot or {}
        securities={r['symbol']:r for r in snapshot.get('securities',[])}
        # Display selection only when it can be computed under current settings.
        signal=None
        if snapshot:
            try:
                from .strategy import compute_signal
                if self.signal_mtime!=self.snapshot_mtime:
                    self.cached_signal=compute_signal(snapshot,self.config);self.signal_mtime=self.snapshot_mtime
                signal=self.cached_signal
            except Exception:error='선정 자료 검증 실패: 주문 신호로 사용할 수 없음'
        if self.workflow and (self.workflow.get('SELECTION',{}).get('status')!='COMPLETE'
            or snapshot.get('selection_date')!=self.workflow_day):signal=None
        selected={r['symbol'] for r in (signal or {}).get('candidates',[]) if r['selected']}
        positions=ledger.positions();orders=ledger.orders()
        from .config import preview_holdings
        virtual={symbol:holding for symbol,holding in preview_holdings(self.config).items() if symbol not in positions}
        for symbol,holding in virtual.items():
            positions[symbol]={'quantity':holding['quantity'],'cost':str(holding['price']*holding['quantity']),'mark':str(holding['price']),'preview':True}
        cycle_day=self.workflow_day if self.workflow else snapshot.get('execution_date')
        cycle=ledger.db.execute("SELECT * FROM runs WHERE day=? AND kind='REBALANCE'",(cycle_day,)).fetchone()
        baseline=snapshot.get('selection_positions',positions) if snapshot.get('execution_date')==cycle_day else positions
        cycle_orders=[]
        if cycle and cycle['state']=='REARMED':signal=None;selected=set();baseline=positions
        if cycle and cycle['state']!='REARMED':
            payload=json.loads(cycle['payload']);baseline=payload.get('selection_positions',baseline)
            cycle_orders=ledger.orders(run_id=cycle['id'])
            for audit in ledger.db.execute("SELECT kind,payload FROM audit WHERE kind IN ('INITIAL_PLAN','POST_SELL_PLAN','REPLACEMENT') ORDER BY id"):
                detail=json.loads(audit['payload'])
                if detail.get('run_id')!=cycle['id']:continue
                if audit['kind'] in ('INITIAL_PLAN','POST_SELL_PLAN'):selected=set(detail['selected'])
                elif any(o['symbol']==detail['new'] and (o['filled']>0 or o['state'] in ('SUBMITTING','SUBMITTED','PARTIALLY_FILLED','UNKNOWN','CANCEL_PENDING')) for o in cycle_orders):
                    selected.discard(detail['old']);selected.add(detail['new'])
        latest={}
        for order in (cycle_orders or orders):latest[order['symbol']]=order
        active_symbols={o['symbol'] for o in ledger.orders(True)}
        symbols=sorted(set(positions)|selected|active_symbols|set(baseline)|{o['symbol'] for o in cycle_orders})
        sold={}
        for order in cycle_orders:sold[order['symbol']]=sold.get(order['symbol'],Decimal(0))+(Decimal(order['gross']) if order['side']=='SELL' else Decimal(0))
        nav=ledger.db.execute('SELECT day,created_at FROM nav ORDER BY day DESC LIMIT 1').fetchone()
        mark_time=nav['day']+' 종가 평가' if nav else '원장 가격 / 평가일 미확인'
        value=Decimal(0);cost=Decimal(0);rows=[];provenance=[]
        table_cost=Decimal(0);table_value=Decimal(0)
        for symbol in symbols:
            p=positions.get(symbol,{});q=quotes.get(symbol,{}) or {};security=securities.get(symbol,{})
            qty=p.get('quantity',0);basis=Decimal(p.get('cost','0'));mark=Decimal(p.get('mark','0'))
            observed=q.get('observed_at');fresh=False
            try:fresh=0<Decimal(str(q['price'])) and 0<=(now-timestamp(observed)).total_seconds()<=self.config.quote_max_age
            except (KeyError,ValueError,TypeError,ArithmeticError):pass
            price=Decimal(str(q['price'])) if fresh else mark
            if p.get('exit_required'):price=Decimal(0)
            amount=price*qty
            if symbol not in virtual:value+=amount;cost+=basis
            regulatory=q.get('regulatory') or security.get('regulatory') or {}
            warnings='/'.join(LABELS[k] for k in FLAGS if regulatory.get(k) is True) or ('없음' if regulatory.get('known') is True else '미확인')
            warning_time=regulatory.get('retrieved_at') or regulatory.get('available_at') or '시점 미확인'
            order=latest.get(symbol,{})
            category=('유지' if symbol in selected else '기존') if symbol in baseline else ('신규' if symbol in selected else '-')
            if cycle and cycle['state']=='COMPLETE' and category=='기존' and qty==0 and symbol not in active_symbols:
                continue
            status=order.get('state') or ('보유중' if qty else '매수대기' if symbol in selected else '청산대기')
            if status=='FILLED' and qty:status='보유중' if order.get('side')=='BUY' else '매도완료'
            if not qty and sold.get(symbol,0)>0 and symbol not in selected:status='청산완료'
            if p.get('exit_required'):status+=' / 회복매도 대기'
            if p.get('valuation_status')=='STALE':status+=' / 평가 지연'
            holding=bool(cycle and cycle['state']=='COMPLETE' and qty>0 and symbol not in active_symbols
                and not p.get('exit_required'))
            if holding:status='Holding'
            profit=amount-basis
            pnl=f'{money(profit)} ({profit/basis*100:.1f})' if basis else '-'
            price_text=money(price) if qty or fresh else '-'
            fallback_time=mark_time
            fill=ledger.db.execute('SELECT occurred_at FROM fills WHERE symbol=? AND qty>0 ORDER BY rowid DESC LIMIT 1',(symbol,)).fetchone()
            if fill and (not nav or timestamp(fill['occurred_at'])>timestamp(nav['created_at'])):
                fallback_time=fill['occurred_at']+' 최종 체결가격'
            action=ledger.db.execute("SELECT occurred_at FROM quantity_events WHERE symbol=? AND type='CORPORATE_ACTION' ORDER BY rowid DESC LIMIT 1",(symbol,)).fetchone()
            if action and (not nav or timestamp(action['occurred_at'])>timestamp(nav['created_at'])) and (not fill or timestamp(action['occurred_at'])>timestamp(fill['occurred_at'])):
                fallback_time=action['occurred_at']+' 권리변동 후 원장 가격'
            if p.get('valuation_status')=='STALE':fallback_time='원장 가격 / 평가 지연·시점 미확인'
            price_time=observed if fresh and not p.get('exit_required') else ('0원 평가 유지' if p.get('exit_required') else fallback_time if qty else '가격 미조회')
            quote_text=(money(q['price'])+' / '+str(observed)) if q.get('price') else '-'
            if symbol in virtual:
                status='가상보유 / 매매대상 아님';holding=True;category='기존';warnings='표시용'
                security=dict(security,name=virtual[symbol]['name'])
            table_cost+=basis;table_value+=amount
            rows.append([symbol,security.get('name') or q.get('name') or '-',
                category,warnings,qty,money(basis/qty) if qty else '-',money(basis),price_text,money(amount),money(sold.get(symbol,0)),pnl,
                status+(f" / {order.get('side','-')} {order.get('filled',0)}/{order.get('quantity',0)}" if order and not holding else '')])
            if symbol in virtual:price_time=observed if fresh else '설정 평단 / 시세 미조회·만료'
            provenance.append(f'{symbol} 평가: {price_time} | 최근 조회가: {quote_text} | 경고자료: {warning_time}')

        table_profit=table_value-table_cost
        total_pnl=money(table_profit)+(f' ({table_profit/table_cost*100:.1f})' if table_cost else ' (-)')
        totals=['합계','','','','','',money(table_cost),'',money(table_value),'',total_pnl,'']
        try:self.config.validate_live();readiness='주문 모드 설정 확인 완료'
        except Exception as exc:readiness='실주문 차단: '+clean(exc)
        external=Decimal(0)
        for row in ledger.db.execute('SELECT amount FROM cash_events WHERE external_flow=1'):external+=Decimal(row['amount'])
        equity=ledger.cash+value
        summary_signal=dict(signal,candidates=[dict(r,selected=r['symbol'] in selected) for r in signal['candidates']]) if signal else None
        lines=[f'전략이름: {self.config.strategy_id} ({self.config.strategy_label})',
            f'시작시간: {self.started:%Y.%m.%d %H:%M:%S} KST',
            f'업데이트: {now:%Y-%m-%d %H:%M:%S} KST',
            f'실행모드: KIS {self.config.environment}',
            selection_summary(summary_signal if not self.workflow or self.workflow.get('SELECTION',{}).get('status')=='COMPLETE' else None),
            *self.schedule(ledger,now,positions),
            *([f'당일 실행시각: {self.config.execution_time} KST (재예약, 당일만 적용)']
              if ledger.meta('rearm_execution_day')==now.astimezone(SEOUL).date().isoformat() else []),
            f'현재처리: {clean(state)}',
            *self.workflow_lines(),
            *([f'주문 차단: {readiness}'] if readiness!='주문 모드 설정 확인 완료' else []), '',
            f'전략현금: {money(ledger.cash)}원 | 주문 예약금: {money(ledger.reserved_cash())}원 | 가용현금: {money(ledger.available_cash())}원',
            f'투자상한: {money(self.config.capital)}원',
            f'누적손익: {money(equity-external)} ('+(f'{(equity-external)/external*100:+.2f}%' if external>0 else '수익률 계산 불가')+')',
            self.recent_trade(ledger), '',
            table(['종목코드','종목명','선정','경고','수량','평단','매수금액','현재금액','평가금액','매도금액','평가손익(%)','상태'],rows,totals), '', f'최근 운영 로그   {now.astimezone(SEOUL):%Y.%m.%d %H:%M:%S} KST']
        if provenance:lines[-1:-1]=['가격·경고 자료 시점']+[clean(line) for line in provenance]+['']
        lines.extend(clean(line) for line in recent[-self.config.dashboard_recent_lines:])
        if not recent:lines.append('새로운 체결·오류·수집 알림 없음')
        return '\n'.join(lines)

    def recent_trade(self,ledger):
        signature=(str(ledger.path),ledger.db.execute('SELECT MAX(rowid) FROM fills').fetchone()[0],
            ledger.db.execute('SELECT MAX(rowid) FROM quantity_events').fetchone()[0])
        if signature!=self.trade_signature:
            self.cached_trade=latest_realized_trade(ledger);self.trade_signature=signature
        trade=self.cached_trade
        if trade is None:return '최근거래: 매도 체결 없음 또는 손익 확인 불가'
        rate=f"{trade['profit']/trade['cost']*100:+.2f}%" if trade['cost']>0 else '수익률 계산 불가'
        day=timestamp(trade['at']).astimezone(SEOUL)
        return f"최근거래: {money(trade['profit'])} ({rate}, {day.year}. {day.month}. {day.day})"

    def show(self,ledger,quotes=None,state='상태 조회',force=False,now=None):
        if not self.config.dashboard_enabled:return
        tick=time.monotonic()
        if not force and self.last_refresh is not None and tick-self.last_refresh<self.config.dashboard_refresh_seconds:return
        self.last_refresh=tick
        from .logs import current
        logger=current()
        try:
            text=self.render(ledger,quotes,state,now=now,recent=list(logger.recent) if logger else [])
            if logger:text=logger.redact(text)
            tty=self.stream.isatty()
            stable='\n'.join(l for l in text.splitlines() if not l.startswith(('업데이트:','최근 운영 로그   ')))
            if tty or force or stable!=self.last_logged:
                output=text
                if tty:
                    output='\n'.join(line.replace('최근 운영 로그   ', '최근 운영 로그   \033[96m',1)+'\033[0m' if line.startswith('최근 운영 로그   ') else line for line in text.splitlines())
                self.stream.write(('\033[H\033[2J' if tty else '')+output+'\n');self.stream.flush()
            if logger and stable!=self.last_logged:logger.write('screen',text)
            self.last_logged=stable
        except Exception:
            if logger:logger.write('system','DASHBOARD_ERROR: 화면 갱신 실패; 매매 상태는 원장에서 확인하세요.')
