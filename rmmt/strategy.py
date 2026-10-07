"""Pure point-in-time signal and portfolio functions; no network or writes."""
from datetime import datetime, date, time, timedelta
from decimal import Decimal, ROUND_FLOOR
from zoneinfo import ZoneInfo
import hashlib, json, math
from .config import RMMTError

SEOUL = ZoneInfo('Asia/Seoul')
FLAGS = ('MANAGED','CAUTIONARY','CAUTION','WARNING','RISK')

def decile_bounds(size,decile):
    """Split ascending market-cap ranks into ten contiguous equal-count groups."""
    if not isinstance(decile,int) or isinstance(decile,bool) or not 0<=decile<=10:
        raise RMMTError('DECILE은 0~10 범위의 정수여야 합니다.')
    if decile==0:return 0,size
    quotient,remainder=divmod(size,10)
    start=(decile-1)*quotient+min(decile-1,remainder)
    return start,start+quotient+(decile<=remainder)

def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise RMMTError('시각에는 UTC offset가 필요합니다.')
    return dt

def decimal(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (ValueError, ArithmeticError):
        raise RMMTError('유효하지 않은 금액/가격') from None

def validate_snapshot(snapshot,config=None):
    formation_days=config.formation_days if config else 21
    market=config.market if config else 'KOSDAQ'
    if snapshot.get('market', 'KOSDAQ')!=market:raise RMMTError('스냅샷 수집 시장과 MARKET 설정 불일치')
    if snapshot.get('schema_version') != 1:
        raise RMMTError('snapshot schema_version=1 필요')
    dates = snapshot['sessions']
    if dates != sorted(set(dates)) or any(date.fromisoformat(d).weekday()>4 for d in dates):
        raise RMMTError('거래일 배열 중복/순서/주말 오류')
    if not snapshot.get('universe_complete') or not snapshot.get('calendar_complete'):
        raise RMMTError('전체 보통주 집단/공식 거래일 달력 완전성 확인 필요')
    signal_date = snapshot['signal_date']
    cutoff = timestamp(snapshot['data_cutoff_at'])
    if cutoff.astimezone(SEOUL).date().isoformat() < signal_date:
        raise RMMTError('데이터 컷오프가 신호일보다 빠릅니다.')
    if signal_date not in dates or dates.index(signal_date) < formation_days:
        raise RMMTError(f'{formation_days}시장거래일 형성이력 부족')
    index = dates.index(signal_date)
    if index+1 >= len(dates):
        raise RMMTError('다음 거래일 달력 필요')
    if snapshot['execution_date'] != dates[index+1]:
        raise RMMTError('실행일은 신호 다음 시장거래일이어야 합니다.')
    if not snapshot['securities']:raise RMMTError('전체 보통주 기준 집단이 비어 있습니다.')
    symbols = set()
    for row in snapshot['securities']:
        symbol = row['symbol']
        if not isinstance(symbol,str) or not symbol or symbol in symbols:
            raise RMMTError('종목코드는 고유 문자열이어야 합니다.')
        symbols.add(symbol)
        if row.get('market') not in (('KOSDAQ','KOSPI') if market=='COMBINED' else (market,)) or row.get('security_type') != 'COMMON':
            raise RMMTError('D1 기준 집단에 비보통주/다른 시장 포함')
        if decimal(row['market_cap']) <= 0 or row.get('market_cap_date') != signal_date:
            raise RMMTError('모든 보통주의 신호일 시총 필요')
        if row.get('classification_source') is None:
            raise RMMTError('공식 보통주 분류 출처 필요')
        daily = row.get('daily',[])
        if len({r['date'] for r in daily}) != len(daily):
            raise RMMTError('일봉 날짜 중복')
        for bar in daily:
            if bar['date'] > signal_date:
                raise RMMTError('신호 스냅샷에 미래 일봉 포함')
            volume=bar.get('volume')
            if volume is not None and (type(volume) is not int or volume<0):raise RMMTError('거래량은 0 이상 정수 또는 null이어야 합니다.')
    return snapshot

def regulatory_reasons(status, at, max_age=None, enabled_flags=FLAGS):
    if not status or status.get('known') is not True or any(type(status.get(flag)) is not bool for flag in FLAGS):
        return ['REGULATORY_STATUS_UNKNOWN']
    try:
        known_at = timestamp(status['available_at'])
        observed = timestamp(status['retrieved_at'])
        effective = timestamp(status['effective_at'])
        valid_until = timestamp(status['valid_until'])
    except (KeyError, ValueError, RMMTError):
        return ['REGULATORY_STATUS_UNKNOWN']
    if known_at > at or observed > at or effective > at or valid_until <= at:
        return ['REGULATORY_STATUS_UNKNOWN']
    if max_age is not None and (at-observed).total_seconds() > max_age:
        return ['REGULATORY_STATUS_UNKNOWN']
    if not status.get('source') or not status.get('snapshot_id'):
        return ['REGULATORY_STATUS_UNKNOWN']
    return ['REGULATORY_'+flag for flag in FLAGS if flag in enabled_flags and status[flag]]

def shock_state(daily):
    """Full causal replay from supplied verified history; no forward scan."""
    state, previous, base, direction, count = 'NORMAL', None, None, 0, 0
    for bar in sorted(daily,key=lambda r:r['date']):
        price, volume = bar.get('split_adjusted_close'), bar.get('volume')
        if price is None or volume is None or volume <= 0:
            continue
        price = decimal(price)
        if price <= 0:
            continue
        if state == 'CONFIRMED_PERSISTENT':
            previous = price
            continue
        if state == 'PENDING_JUMP':
            beyond = price > base*Decimal('1.35') if direction>0 else price < base*Decimal('.65')
            if not beyond:
                state, count = 'NORMAL', 0
            else:
                count += 1
                if count == 5:
                    state = 'CONFIRMED_PERSISTENT'
        elif previous is not None:
            change = price/previous-1
            if abs(change) > Decimal('.35'):
                state, base, direction, count = 'PENDING_JUMP', previous, 1 if change>0 else -1, 0
        previous = price
    return {'state':state,'baseline':str(base) if base is not None else None,'observations':count}

def compute_signal(snapshot, config):
    validate_snapshot(snapshot,config)
    cutoff = timestamp(snapshot['data_cutoff_at'])
    sessions = snapshot['sessions']; end = sessions.index(snapshot['signal_date'])
    formation = sessions[end-config.formation_days:end+1]
    universe = sorted(snapshot['securities'],key=lambda r:(decimal(r['market_cap']),r['symbol']))
    start,stop=decile_bounds(len(universe),config.decile)
    count=stop-start
    decisions, candidates = [], []
    for rank,row in enumerate(universe[start:stop],start+1):
        symbol = row['symbol']; bars = {r['date']:r for r in row.get('daily',[])}
        reasons=[]
        values = [bars.get(d,{}).get('split_adjusted_close') for d in formation]
        if row.get('adjustment_basis') != 'SPLIT_ONLY' or row.get('signal_source') != 'YAHOO' or any(v is None or decimal(v)<=0 for v in values):
            reasons.append('SIGNAL_HISTORY_UNKNOWN')
        close = bars.get(snapshot['signal_date'],{}).get('nominal_close')
        if close is None or decimal(close) < config.minimum_price:
            reasons.append('MIN_PRICE')
        volumes=[bars.get(d,{}).get('volume') for d in sessions[end-4:end+1]]
        if any(v is None for v in volumes):
            reasons.append('VOLUME_UNKNOWN')
        elif all(v==0 for v in volumes) or volumes[-1]==0:
            reasons.append('NO_TRADE')
        if row.get('shock_history_complete') is not True:
            reasons.append('SHOCK_HISTORY_UNKNOWN')
        shock=shock_state(row.get('daily',[]))
        if shock['state'] != 'NORMAL':
            reasons.append(shock['state'])
        if row.get('tradable_on_signal') is not True:
            reasons.append('TRADING_STATUS_UNKNOWN_OR_BLOCKED')
        reasons.extend(regulatory_reasons(row.get('regulatory'),cutoff,enabled_flags=config.excluded_regulatory_flags()))
        decisions.append({'symbol':symbol,'cap_rank':rank,'decile':config.decile,'reasons':reasons,'result':'FAIL' if reasons else 'PASS','shock':shock})
        if not reasons:
            momentum=decimal(values[-1])/decimal(values[0])-1
            candidates.append({'symbol':symbol,'formation_return':str(momentum),'cap_rank':rank})
    candidates.sort(key=lambda r:(Decimal(r['formation_return']),r['symbol']))
    for rank,row in enumerate(candidates,1):
        row.update(rank=rank,selected=rank<=config.holdings)
    identity=hashlib.sha256(json.dumps({'snapshot':snapshot,'config':config.public()},sort_keys=True).encode()).hexdigest()
    return {'signal_id':identity,'signal_date':snapshot['signal_date'],'execution_date':snapshot['execution_date'],
            'data_cutoff_at':snapshot['data_cutoff_at'],'config_hash':config.hash,
            'universe_size':len(universe),'d1_size':len(universe)//10+bool(len(universe)%10),
            'decile':config.decile,'decile_size':count,'candidates':candidates,'decisions':decisions}

def quote_reasons(quote, at, config, buy=True):
    reasons=[]
    if not quote or quote.get('tradable') is not True:
        reasons.append('NOT_TRADABLE')
    try:
        age=(at-timestamp(quote['observed_at'])).total_seconds()
        if not 0 <= age <= config.quote_max_age or decimal(quote['price'])<=0:
            reasons.append('QUOTE_STALE_OR_INVALID')
    except (KeyError,ValueError,RMMTError):
        reasons.append('QUOTE_UNKNOWN')
    if buy:
        if (quote or {}).get('traded_today') is not True:reasons.append('NO_CURRENT_TRADE_OBSERVATION')
        reasons.extend(regulatory_reasons((quote or {}).get('regulatory'),at,config.status_max_age,config.excluded_regulatory_flags()))
    return reasons

def select_execution(signal, positions, quotes, at, config):
    selected, checks=[],[]
    for candidate in signal['candidates']:
        symbol=candidate['symbol']
        reasons=quote_reasons(quotes.get(symbol),at,config)
        if positions.get(symbol,{}).get('exit_required'):
            reasons.append('EXIT_REQUIRED')
        checks.append({'symbol':symbol,'reasons':reasons})
        if not reasons and len(selected)<config.holdings:
            selected.append(symbol)
    return selected, checks

def plan_rebalance(signal, positions, cash, quotes, at, config):
    selected, checks=select_execution(signal,positions,quotes,at,config)
    equity=decimal(cash)
    frozen=Decimal(0)
    orders=[]
    for symbol,p in positions.items():
        qty=p['quantity']; price=Decimal(0) if p.get('exit_required') else decimal(p['mark'])
        equity+=qty*price
        if quote_reasons(quotes.get(symbol),at,config,buy=False):
            frozen+=qty*price
    deploy=max(Decimal(0),min(equity,config.capital)-frozen)
    target=deploy/len(selected) if selected else Decimal(0)
    for symbol in sorted(set(positions)|set(selected)):
        current=positions.get(symbol,{}).get('quantity',0)
        quote=quotes.get(symbol)
        if quote_reasons(quote,at,config,buy=False):
            continue
        price=decimal(quote['price'])
        wanted=int((target/(price*(1+config.buy_fee))).to_integral_value(rounding=ROUND_FLOOR)) if symbol in selected else 0
        delta=wanted-current
        if delta:
            orders.append({'symbol':symbol,'side':'BUY' if delta>0 else 'SELL','quantity':abs(delta),
                           'current_qty':current,'target_qty':wanted,'reference_price':str(price),'target_value':str(target)})
    return {'signal_id':signal['signal_id'],'selected':selected,'checks':checks,'equity':str(equity),
            'frozen_value':str(frozen),'deployable':str(deploy),'target_value':str(target),
            'sells':[o for o in orders if o['side']=='SELL'],'buys':[o for o in orders if o['side']=='BUY']}

def execution_window(day, config):
    start=datetime.combine(date.fromisoformat(day),time.fromisoformat(config.execution_time),SEOUL)
    return start, start+timedelta(minutes=config.window_minutes)


def rebalance_dates(calendar,config):
    sessions=calendar['sessions']
    if calendar['start']>config.rebalance_anchor_date:
        raise RMMTError('리밸런싱 기준일을 포함하는 공식 달력이 필요합니다.')
    eligible=[d for d in sessions if d>=config.rebalance_anchor_date]
    return eligible[::config.rebalance_days]
