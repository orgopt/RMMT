"""Transactional strategy-owned quantity/cash ledger with cumulative fill replay."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import fcntl, json, sqlite3
from pathlib import Path
from .config import RMMTError, local_path
from .strategy import decimal

ACTIVE=('PLANNED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_PENDING')
TERMINAL=('FILLED','CANCELLED','REJECTED')
def now():
    return datetime.now(timezone.utc).isoformat()
def money(value):
    return decimal(value).quantize(Decimal('1'),rounding=ROUND_HALF_UP)

@contextmanager
def process_lock(path):
    path=local_path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RMMTError('다른 RMMT 프로세스가 원장을 사용 중입니다.') from None
        try:
            yield
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)

class Ledger:
    def __init__(self,path,config,create=True,readonly=False):
        self.config=config; self.path=local_path(path)
        if not create and not self.path.exists():
            raise RMMTError('원장이 없습니다. 먼저 init-ledger를 실행하세요.')
        if readonly:
            from urllib.parse import quote
            self.db=sqlite3.connect('file:'+quote(str(self.path))+'?mode=ro',uri=True,timeout=10)
            self.db.row_factory=sqlite3.Row
            if self.meta('mode')!=config.mode:raise RMMTError('원장 모드 불일치')
            return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY,quantity INTEGER NOT NULL CHECK(quantity>=0),
          cost TEXT NOT NULL,mark TEXT NOT NULL,exit_required INTEGER NOT NULL DEFAULT 0,valuation_status TEXT NOT NULL DEFAULT 'NORMAL');
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,day TEXT NOT NULL,kind TEXT NOT NULL,
          state TEXT NOT NULL,config_hash TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(day,kind));
        CREATE TABLE IF NOT EXISTS orders(intent_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),symbol TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),quantity INTEGER NOT NULL CHECK(quantity>0),
          price_bound TEXT NOT NULL,state TEXT NOT NULL,broker_id TEXT,branch TEXT,filled INTEGER NOT NULL DEFAULT 0,
          gross TEXT NOT NULL DEFAULT '0',fee TEXT NOT NULL DEFAULT '0',tax TEXT NOT NULL DEFAULT '0',reserved TEXT NOT NULL,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,reason TEXT);
        DROP INDEX IF EXISTS broker_unique;
        CREATE UNIQUE INDEX IF NOT EXISTS broker_run_unique ON orders(run_id,broker_id,branch) WHERE broker_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY,intent_id TEXT NOT NULL REFERENCES orders(intent_id),
          symbol TEXT NOT NULL,side TEXT NOT NULL,qty INTEGER NOT NULL,gross TEXT NOT NULL,fee TEXT NOT NULL,tax TEXT NOT NULL,
          occurred_at TEXT NOT NULL,simulated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS cash_events(id TEXT PRIMARY KEY,type TEXT NOT NULL,amount TEXT NOT NULL,
          external_flow INTEGER NOT NULL,occurred_at TEXT NOT NULL,source_id TEXT);
        CREATE TABLE IF NOT EXISTS quantity_events(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,delta INTEGER NOT NULL,
          type TEXT NOT NULL,occurred_at TEXT NOT NULL,source_id TEXT);
        CREATE TABLE IF NOT EXISTS valuation_events(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,day TEXT NOT NULL,
          old_mark TEXT NOT NULL,new_mark TEXT NOT NULL,reason TEXT NOT NULL,quantity INTEGER NOT NULL,occurred_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS nav(day TEXT PRIMARY KEY,cash TEXT NOT NULL,reserved_cash TEXT NOT NULL,
          holdings_value TEXT NOT NULL,equity TEXT NOT NULL,normalized_nav TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,occurred_at TEXT NOT NULL);
        ''')
        if not self.meta('initial_equity'):
            with self.db:
                self.set_meta('initial_equity',str(config.capital));self.set_meta('cash',str(config.capital))
                self.set_meta('capital_limit',str(config.capital));self.set_meta('mode',config.mode)
                self.db.execute('INSERT INTO cash_events VALUES(?,?,?,?,?,?)',('initial','ALLOCATION',str(config.capital),1,now(),None))
        if self.meta('mode') != config.mode:
            raise RMMTError('원장 모드 불일치: PAPER/DEMO/LIVE 별도 DB가 필요합니다.')
        old_limit=self.meta('capital_limit')
        if Decimal(old_limit) != config.capital:
            with self.db:
                self.set_meta('capital_limit',str(config.capital))
                self.db.execute('INSERT INTO audit(kind,payload,occurred_at) VALUES(?,?,?)',
                    ('CAPITAL_LIMIT_CHANGE',json.dumps({'old':old_limit,'new':str(config.capital),'source':'ENV'},ensure_ascii=False),now()))
            from .logs import event
            event(f'투자상한 변경: {Decimal(old_limit):,.0f}원 → {config.capital:,.0f}원 (.env 반영)')

    def close(self):
        from .logs import sync
        try:sync(self)
        finally:self.db.close()
    def meta(self,key):
        row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return row[0] if row else None
    def set_meta(self,key,value):
        self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))
    @property
    def cash(self): return Decimal(self.meta('cash'))
    def positions(self):
        return {r['symbol']:dict(r) for r in self.db.execute('SELECT * FROM positions WHERE quantity>0')}
    def orders(self,active=False,run_id=None):
        query='SELECT * FROM orders';args=[];conditions=[]
        if active:
            conditions.append('state IN ('+','.join('?'*len(ACTIVE))+')');args.extend(ACTIVE)
        if run_id:
            conditions.append('run_id=?');args.append(run_id)
        if conditions:query+=' WHERE '+' AND '.join(conditions)
        return [dict(r) for r in self.db.execute(query+' ORDER BY created_at,intent_id',args)]
    def reserved_cash(self):
        return sum((Decimal(o['reserved']) for o in self.orders(True) if o['side']=='BUY'),Decimal(0))
    def available_cash(self): return self.cash-self.reserved_cash()
    def available_quantity(self,symbol):
        qty=self.positions().get(symbol,{}).get('quantity',0)
        return qty-sum(o['quantity']-o['filled'] for o in self.orders(True) if o['symbol']==symbol and o['side']=='SELL')
    def audit(self,kind,payload):
        with self.db:self.db.execute('INSERT INTO audit(kind,payload,occurred_at) VALUES(?,?,?)',(kind,json.dumps(payload,ensure_ascii=False,default=str),now()))
    def begin_run(self,run_id,day,kind,payload):
        existing=self.db.execute('SELECT * FROM runs WHERE day=? AND kind=?',(day,kind)).fetchone()
        if existing:
            if existing['id']!=run_id or existing['config_hash']!=self.config.hash:
                raise RMMTError('같은 거래일 실행의 신호/설정 변경은 허용하지 않습니다.')
            return dict(existing),False
        with self.db:
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',(run_id,day,kind,'STARTED',self.config.hash,json.dumps(payload,ensure_ascii=False),now()))
        return dict(self.db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()),True
    def finish_run(self,run_id,state):
        with self.db:self.db.execute('UPDATE runs SET state=? WHERE id=?',(state,run_id))
    def run(self,run_id):
        row=self.db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        return dict(row) if row else None
    def create_order(self,run_id,symbol,side,qty,price_bound,sequence=0):
        if type(qty) is not int or qty<=0 or side not in ('BUY','SELL') or decimal(price_bound)<=0:
            raise RMMTError('주문 수량/방향/가격 오류')
        key=f'{run_id}:{symbol}:{side}:{sequence}'
        row=self.db.execute('SELECT * FROM orders WHERE intent_id=?',(key,)).fetchone()
        if row:return dict(row),False
        bound=decimal(price_bound)
        reserve=money(qty*bound*(1+self.config.buy_fee)) if side=='BUY' else Decimal(0)
        with self.db:
            if side=='BUY' and reserve>self.available_cash():raise RMMTError('전략 현금 예약 초과')
            if side=='SELL' and qty>self.available_quantity(symbol):raise RMMTError('전략 귀속수량 예약 초과')
            self.db.execute('INSERT INTO orders(intent_id,run_id,symbol,side,quantity,price_bound,state,reserved,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key,run_id,symbol,side,qty,str(bound),'PLANNED',str(reserve),now(),now()))
        return dict(self.db.execute('SELECT * FROM orders WHERE intent_id=?',(key,)).fetchone()),True
    def transition(self,intent,state,broker_id=None,branch=None,reason=None):
        row=self.db.execute('SELECT * FROM orders WHERE intent_id=?',(intent,)).fetchone()
        if not row:raise RMMTError('주문 의도 없음')
        legal={'PLANNED':('SUBMITTING','REJECTED','CANCELLED'),
               'SUBMITTING':('ACKNOWLEDGED','REJECTED','UNKNOWN'),
               'UNKNOWN':('ACKNOWLEDGED','UNKNOWN'),
               'ACKNOWLEDGED':('CANCEL_PENDING','UNKNOWN'),
               'PARTIALLY_FILLED':('CANCEL_PENDING','UNKNOWN'),
               'CANCEL_PENDING':('CANCEL_PENDING','UNKNOWN','ACKNOWLEDGED','PARTIALLY_FILLED')}
        if row['state']!=state and state not in legal.get(row['state'],()):
            raise RMMTError(f'주문 상태 전이 오류 {row["state"]}->{state}')
        if state=='ACKNOWLEDGED' and not (broker_id or row['broker_id']):raise RMMTError('주문번호 없는 접수 금지')
        if broker_id:
            duplicate=self.db.execute("SELECT o.intent_id FROM orders o JOIN runs r ON o.run_id=r.id WHERE o.broker_id=? AND o.branch=? AND r.day=(SELECT day FROM runs WHERE id=?) AND o.intent_id<>?",
                (broker_id,branch,row['run_id'],intent)).fetchone()
            if duplicate:raise RMMTError('같은 거래일 브로커 주문이 이미 다른 의도에 귀속되어 있습니다.')
        with self.db:
            self.db.execute('UPDATE orders SET state=?,broker_id=COALESCE(?,broker_id),branch=COALESCE(?,branch),reason=?,updated_at=?,reserved=? WHERE intent_id=?',
                (state,broker_id,branch,reason,now(),'0' if state in TERMINAL else row['reserved'],intent))
        from .logs import event
        event(f'ORDER_STATE mode={self.config.mode} intent={intent} state={state} reason={reason}')
    def apply_cumulative(self,intent,qty,gross,terminal=None,fee=None,tax=None,simulated=False):
        """Apply broker cumulative quantity/amount delta atomically, including cancel races."""
        row=self.db.execute('SELECT * FROM orders WHERE intent_id=?',(intent,)).fetchone()
        if not row or type(qty) is not int or qty<0:raise RMMTError('체결 계약 오류')
        gross=decimal(gross)
        if qty>row['quantity'] or qty<row['filled'] or gross<Decimal(row['gross']):
            raise RMMTError('누적 체결 역행/과체결')
        if terminal not in (None,'FILLED','CANCELLED','REJECTED'):raise RMMTError('최종 상태 오류')
        if terminal=='FILLED' and qty!=row['quantity']:raise RMMTError('전량체결 수량 불일치')
        if terminal=='REJECTED' and qty:raise RMMTError('체결된 주문은 거절로 확정할 수 없습니다.')
        cumulative_fee=money(gross*(self.config.buy_fee if row['side']=='BUY' else self.config.sell_fee)) if fee is None else money(fee)
        cumulative_tax=money(gross*self.config.sell_tax) if row['side']=='SELL' and tax is None else money(tax or 0)
        if min(gross,cumulative_fee,cumulative_tax)<0 or (qty==0 and gross!=0):raise RMMTError('체결 금액/비용 오류')
        delta_qty=qty-row['filled'];delta_gross=gross-Decimal(row['gross'])
        delta_fee=cumulative_fee-Decimal(row['fee']);delta_tax=cumulative_tax-Decimal(row['tax'])
        if delta_qty==0 and delta_gross!=0:raise RMMTError('체결금액 변경은 별도 대조가 필요합니다.')
        if delta_qty>0 and delta_gross<=0:raise RMMTError('체결금액 필요')
        position=self.positions().get(row['symbol'],{'quantity':0,'cost':'0','mark':'0','exit_required':0,'valuation_status':'NORMAL'})
        cash_delta=-delta_gross-delta_fee if row['side']=='BUY' else delta_gross-delta_fee-delta_tax
        quantity=position['quantity']+(delta_qty if row['side']=='BUY' else -delta_qty)
        if quantity<0:raise RMMTError('타 전략 수량 매도 감지')
        state=terminal or ('FILLED' if qty==row['quantity'] else 'PARTIALLY_FILLED' if qty else row['state'])
        if row['state'] in TERMINAL and state not in TERMINAL:raise RMMTError('종료 주문 역행')
        reserve=Decimal(0) if state in TERMINAL else money((row['quantity']-qty)*Decimal(row['price_bound'])*(1+self.config.buy_fee)) if row['side']=='BUY' else Decimal(0)
        if row['side']=='BUY':cost=Decimal(position['cost'])+delta_gross+delta_fee
        else:cost=Decimal(position['cost'])*Decimal(quantity)/position['quantity'] if position['quantity'] else Decimal(0)
        event=f'{intent}:cumulative:{qty}:{cumulative_fee}:{cumulative_tax}'
        with self.db:
            if delta_qty or delta_fee or delta_tax:
                self.db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)',(event,intent,row['symbol'],row['side'],delta_qty,str(delta_gross),str(delta_fee),str(delta_tax),now(),int(simulated)))
                self.db.execute('INSERT INTO cash_events VALUES(?,?,?,?,?,?)',(event,'FILL' if delta_qty else 'COST_RECONCILIATION',str(cash_delta),0,now(),intent))
                if delta_qty:self.db.execute('INSERT INTO quantity_events VALUES(?,?,?,?,?,?)',(event,row['symbol'],delta_qty if row['side']=='BUY' else -delta_qty,'FILL',now(),intent))
                self.set_meta('cash',self.cash+cash_delta)
                mark=Decimal(position['mark'])
                if delta_qty and not position['exit_required']:mark=delta_gross/delta_qty
                self.db.execute('INSERT INTO positions VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,cost=excluded.cost,mark=excluded.mark,exit_required=excluded.exit_required,valuation_status=excluded.valuation_status',
                    (row['symbol'],quantity,str(cost),str(mark),int(position['exit_required']) if quantity else 0,position['valuation_status'] if quantity else 'NORMAL'))
            self.db.execute('UPDATE orders SET filled=?,gross=?,fee=?,tax=?,state=?,reserved=?,updated_at=? WHERE intent_id=?',
                (qty,str(gross),str(cumulative_fee),str(cumulative_tax),state,str(reserve),now(),intent))
        from .logs import sync,event
        sync(self)
        event(f'FILL_UPDATE mode={self.config.mode} intent={intent} cumulative_qty={qty} state={state}')
        # Real overspend must still be booked. Freeze later orders rather than discard a real fill.
        return self.validate()
    def mark_daily(self,day,bars,session_dates):
        with self.db:
            for symbol,p in self.positions().items():
                series=bars.get(symbol,{})
                last=[series.get(d,{}).get('volume') for d in session_dates[-5:]]
                zero=len(last)==5 and all(v is not None and v==0 for v in last)
                delisted=series.get(day,{}).get('delisted_confirmed') is True
                price=series.get(day,{}).get('nominal_close')
                if zero or delisted:
                    if not p['exit_required']:
                        key=f'ZERO:{symbol}:{day}'
                        self.db.execute('INSERT INTO valuation_events VALUES(?,?,?,?,?,?,?,?)',(key,symbol,day,p['mark'],'0','DELISTED' if delisted else 'NO_TRADE_5',p['quantity'],now()))
                    self.db.execute("UPDATE positions SET mark='0',exit_required=1,valuation_status=? WHERE symbol=?",('DELISTED' if delisted else 'ZERO_NO_TRADE',symbol))
                elif not p['exit_required'] and price is not None and decimal(price)>0:
                    self.db.execute("UPDATE positions SET mark=?,valuation_status='NORMAL' WHERE symbol=?",(str(decimal(price)),symbol))
                elif not p['exit_required']:
                    self.db.execute("UPDATE positions SET valuation_status='STALE' WHERE symbol=?",(symbol,))
            self.record_nav(day)
    def record_nav(self,day):
        value=sum((p['quantity']*Decimal(p['mark']) for p in self.positions().values()),Decimal(0))
        equity=self.cash+value
        # Cash transfers require TWR before a new normalized NAV can be reported.
        normalized=equity/Decimal(self.meta('initial_equity'))
        self.db.execute('INSERT INTO nav VALUES(?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET cash=excluded.cash,reserved_cash=excluded.reserved_cash,holdings_value=excluded.holdings_value,equity=excluded.equity,normalized_nav=excluded.normalized_nav,created_at=excluded.created_at',
            (day,str(self.cash),str(self.reserved_cash()),str(value),str(equity),str(normalized),now()))
    def cash_event(self,event_id,kind,amount):
        if kind not in ('DIVIDEND','FEE_ADJUSTMENT','RECOVERY'):raise RMMTError('지원되지 않는 현금 사건')
        amount=money(amount)
        if kind in ('DIVIDEND','RECOVERY') and amount<0:raise RMMTError('입금 사건은 음수 불가')
        with self.db:
            if self.db.execute('SELECT 1 FROM cash_events WHERE id=?',(event_id,)).fetchone():return False
            self.db.execute('INSERT INTO cash_events VALUES(?,?,?,?,?,?)',(event_id,kind,str(amount),0,now(),event_id))
            self.set_meta('cash',self.cash+amount)
        return True
    def corporate_action(self,event_id,symbol,new_quantity,verified_reason):
        p=self.positions().get(symbol)
        if not p or type(new_quantity) is not int or new_quantity<0 or not verified_reason:raise RMMTError('권리수량 변경 계약 오류')
        if any(o['symbol']==symbol for o in self.orders(True)):raise RMMTError('미완료 주문 종목 권리수량 변경 금지')
        with self.db:
            if self.db.execute('SELECT 1 FROM quantity_events WHERE id=?',(event_id,)).fetchone():return False
            self.db.execute('INSERT INTO quantity_events VALUES(?,?,?,?,?,?)',(event_id,symbol,new_quantity-p['quantity'],'CORPORATE_ACTION',now(),verified_reason))
            mark=Decimal(p['mark'])*p['quantity']/new_quantity if new_quantity else Decimal(0)
            self.db.execute('UPDATE positions SET quantity=?,mark=? WHERE symbol=?',(new_quantity,str(mark),symbol))
        return True
    def validate(self):
        problems=[]
        total=sum((Decimal(r[0]) for r in self.db.execute('SELECT amount FROM cash_events')),Decimal(0))
        if total!=self.cash:problems.append('CASH_EVENT_MISMATCH')
        for p in (dict(r) for r in self.db.execute('SELECT * FROM positions')):
            symbol=p['symbol']
            qty=self.db.execute('SELECT COALESCE(SUM(delta),0) FROM quantity_events WHERE symbol=?',(symbol,)).fetchone()[0]
            if qty!=p['quantity']:problems.append('QUANTITY_EVENT_MISMATCH:'+symbol)
            if self.available_quantity(symbol)<0:problems.append('SELL_RESERVATION_EXCEEDS_OWN_QTY:'+symbol)
        if self.available_cash()<0:problems.append('CASH_OR_RESERVATION_NEGATIVE')
        return {'passed':not problems,'problems':problems}
    def summary(self):
        return {'mode':self.config.mode,'cash':str(self.cash),'reserved_cash':str(self.reserved_cash()),
                'positions':self.positions(),'orders':self.orders(),'validation':self.validate()}
