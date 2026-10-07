from dataclasses import dataclass, asdict
from decimal import Decimal
from pathlib import Path
import hashlib, hmac, json, os

ROOT = Path(__file__).resolve().parents[1]
class RMMTError(RuntimeError):
    pass

def local_path(value):
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT):
        raise RMMTError('운영 파일 경로는 RMMT 폴더 안이어야 합니다.')
    return path

def read_env(path):
    result = {}
    if path.exists():
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            if '=' not in line:
                raise RMMTError(f'.env {number}행: KEY=VALUE 형식 필요')
            key, value = line.split('=', 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'':
                value = value[1:-1]
            else:
                value = value.split(' #', 1)[0].rstrip()
            result[key.strip()] = value
    return result

@dataclass(frozen=True)
class Config:
    mode: str = 'DRY_RUN'
    environment: str = 'real'
    capital: Decimal = Decimal('10000000')
    capital_policy: str = 'FIXED_CAP'
    holdings: int = 20
    preview_extra_holdings: str = ''
    preview_holding_enabled: bool = False
    preview_holding_symbol: str = '005930'
    preview_holding_name: str = '삼성전자'
    preview_holding_quantity: int = 10
    preview_holding_price: Decimal = Decimal('271500')
    market: str = 'KOSDAQ'
    decile: int = 1
    formation_days: int = 21
    rebalance_days: int = 5
    rebalance_anchor_date: str = '2026-10-12'
    minimum_price: Decimal = Decimal('1000')
    buy_fee: Decimal = Decimal('.00015')
    sell_fee: Decimal = Decimal('.00015')
    sell_tax: Decimal = Decimal('.002')
    exclude_managed: bool = True
    exclude_cautionary: bool = True
    exclude_caution: bool = True
    exclude_warning: bool = True
    exclude_risk: bool = True
    order_policy: str = 'MARKET'
    execution_time: str = '10:00'
    window_minutes: int = 30
    shock_policy: str = 'UNRESOLVED'
    dividend_policy: str = 'UNRESOLVED'
    design_approved: bool = False
    limit_policy: str = 'UNRESOLVED'
    live_enabled: bool = False
    account_policy: str = 'UNRESOLVED'
    account_isolation_verified: bool = False
    bcso_stop_confirmed: bool = False
    bcso_ownership_fix_confirmed: bool = False
    rest_interval: float = .2
    poll_seconds: float = 3
    quote_max_age: int = 30
    status_max_age: int = 60
    strategy_id: str = 'KOSDAQ_D1_21D_S0_5D_N20'
    data_dir: str = 'rmmt/data'
    results_dir: str = 'rmmt/results'
    calendar_path: str = 'rmmt/data/calendar.json'
    snapshot_path: str = 'rmmt/data/signal_latest.json'
    daily_path: str = 'rmmt/data/daily_latest.json'
    quotes_path: str = 'rmmt/data/examples/synthetic_quotes.json'
    db_dry_run: str = 'rmmt/data/dry_run.sqlite'
    db_paper: str = 'rmmt/data/paper.sqlite'
    db_demo: str = 'rmmt/data/demo.sqlite'
    db_live: str = 'rmmt/data/live.sqlite'
    token_real_path: str = 'rmmt/data/.kis_token_real.json'
    token_demo_path: str = 'rmmt/data/.kis_token_demo.json'
    auto_collect: bool = False
    idle_seconds: float = 30
    error_retry_seconds: float = 15
    connect_timeout: float = 5
    request_timeout: float = 20
    public_timeout: float = 30
    hash_timeout: float = 15
    token_margin_seconds: int = 60
    token_cooldown_seconds: int = 60
    master_cache_seconds: int = 45
    account_max_pages: int = 100
    history_max_pages: int = 500
    history_overlap_days: int = 10
    history_cache_gap_days: int = 100
    progress_interval: int = 100
    calendar_max_days: int = 2000
    data_ready_time: str = '16:00'
    system_log_path: str = 'rmmt/logs/system.log'
    trading_log_path: str = 'rmmt/logs/trading.log'
    screen_log_path: str = 'rmmt/logs/screen.log'
    log_max_bytes: int = 10485760
    log_backup_count: int = 5
    dashboard_enabled: bool = True
    dashboard_quote_refresh_seconds: float = 5
    dashboard_refresh_seconds: float = 3
    dashboard_recent_lines: int = 10
    bootstrap_past_days: int = 120
    bootstrap_future_days: int = 45
    preparation_state_path: str = 'rmmt/data/preparation_state.json'

    def excluded_regulatory_flags(self):
        return tuple(flag for flag in ('MANAGED','CAUTIONARY','CAUTION','WARNING','RISK') if getattr(self,'exclude_'+flag.lower()))

    def database_path(self):
        return getattr(self,'db_'+self.mode.lower())

    def public(self):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    @property
    def hash(self):
        return hashlib.sha256(json.dumps(self.public(), sort_keys=True).encode()).hexdigest()

    def validate_live(self):
        if self.mode not in ('LIVE', 'DEMO'):
            raise RMMTError('브로커 주문은 LIVE/DEMO 모드가 필요합니다.')
        if self.mode == 'LIVE' and (self.environment != 'real' or not self.live_enabled):
            raise RMMTError('LIVE는 real 환경 및 RMMT_LIVE_ENABLED=true 별도 승인이 필요합니다.')
        if self.mode == 'DEMO' and self.environment != 'demo':
            raise RMMTError('DEMO는 KIS_ENV=demo가 필요합니다.')
        if self.mode=='LIVE':
            if self.account_policy=='SEPARATE' and not self.account_isolation_verified:raise RMMTError('BCSO와 다른 실계좌인지 확인되지 않았습니다.')
            if self.account_policy=='BCSO_STOPPED' and not self.bcso_stop_confirmed:raise RMMTError('BCSO 중지 확인이 필요합니다.')
            if self.account_policy=='SHARED_OWNED_ONLY' and not self.bcso_ownership_fix_confirmed:raise RMMTError('같은 계좌 병행 전 BCSO 자기 귀속수량 매도 수정 완료 확인이 필요합니다.')
            if self.account_policy not in ('SEPARATE','BCSO_STOPPED','SHARED_OWNED_ONLY'):raise RMMTError('BCSO 전체계좌 매도 충돌: 계좌 분리 또는 BCSO 중지 정책 확정 필요')
        if not self.design_approved or self.shock_policy == 'UNRESOLVED' or self.dividend_policy == 'UNRESOLVED':
            raise RMMTError('미확정 전략 정책이 있습니다. 운영 문서 결정표를 확인하세요.')
        if self.order_policy == 'LIMIT' and self.limit_policy != 'CURRENT_NO_CHASE':
            raise RMMTError('LIMIT 상세 정책이 미확정입니다.')

REGULATORY_SETTINGS={
    'exclude_managed':'EXCLUDE_REGULATORY_MANAGED',
    'exclude_cautionary':'EXCLUDE_REGULATORY_CAUTIONARY',
    'exclude_caution':'EXCLUDE_REGULATORY_CAUTION',
    'exclude_warning':'EXCLUDE_REGULATORY_WARNING',
    'exclude_risk':'EXCLUDE_REGULATORY_RISK'}

PATH_SETTINGS={'data_dir':'RMMT_DATA_DIR','results_dir':'RMMT_RESULTS_DIR','calendar_path':'CALENDAR_PATH',
    'snapshot_path':'SIGNAL_SNAPSHOT_PATH','daily_path':'DAILY_SNAPSHOT_PATH','quotes_path':'QUOTE_SNAPSHOT_PATH',
    'db_dry_run':'DRY_RUN_DB_PATH','db_paper':'PAPER_DB_PATH','db_demo':'DEMO_DB_PATH','db_live':'LIVE_DB_PATH',
    'token_real_path':'KIS_REAL_TOKEN_CACHE_PATH','token_demo_path':'KIS_DEMO_TOKEN_CACHE_PATH','system_log_path':'SYSTEM_LOG_PATH',
    'trading_log_path':'TRADING_LOG_PATH','screen_log_path':'SCREEN_LOG_PATH','preparation_state_path':'PREPARATION_STATE_PATH'}
FLOAT_SETTINGS={'dashboard_quote_refresh_seconds':'DASHBOARD_QUOTE_REFRESH_SECONDS','dashboard_refresh_seconds':'DASHBOARD_REFRESH_SECONDS','idle_seconds':'SERVICE_IDLE_SECONDS','error_retry_seconds':'SERVICE_ERROR_RETRY_SECONDS',
    'connect_timeout':'HTTP_CONNECT_TIMEOUT_SECONDS','request_timeout':'KIS_REQUEST_TIMEOUT_SECONDS',
    'public_timeout':'PUBLIC_DATA_TIMEOUT_SECONDS','hash_timeout':'KIS_HASH_TIMEOUT_SECONDS'}
INT_SETTINGS={'bootstrap_past_days':'BOOTSTRAP_CALENDAR_PAST_DAYS','bootstrap_future_days':'BOOTSTRAP_CALENDAR_FUTURE_DAYS','dashboard_recent_lines':'DASHBOARD_RECENT_LINES','token_margin_seconds':'KIS_TOKEN_EXPIRY_MARGIN_SECONDS','token_cooldown_seconds':'KIS_TOKEN_ISSUE_COOLDOWN_SECONDS',
    'master_cache_seconds':'KIS_MASTER_CACHE_SECONDS','account_max_pages':'KIS_ACCOUNT_MAX_PAGES',
    'history_max_pages':'KIS_HISTORY_MAX_PAGES','history_overlap_days':'KIS_HISTORY_OVERLAP_DAYS',
    'history_cache_gap_days':'KIS_HISTORY_CACHE_MAX_GAP_DAYS','progress_interval':'COLLECTION_PROGRESS_INTERVAL',
    'calendar_max_days':'CALENDAR_MAX_RANGE_DAYS','log_max_bytes':'LOG_MAX_BYTES','log_backup_count':'LOG_BACKUP_COUNT'}

def preview_holdings(config):
    if not config.preview_holding_enabled:return {}
    result={config.preview_holding_symbol: {'name':config.preview_holding_name,'quantity':config.preview_holding_quantity,'price':config.preview_holding_price}}
    for item in config.preview_extra_holdings.split(';'):
        if not item.strip():continue
        try:
            symbol,name,quantity,price=item.strip().split(',')
            quantity=int(quantity);price=Decimal(price)
            if len(symbol)!=6 or not symbol.isdigit() or not name or quantity<=0 or not price.is_finite() or price<=0 or symbol in result:raise ValueError()
        except (ValueError, ArithmeticError):raise RMMTError('PREVIEW_EXTRA_HOLDINGS는 종목코드,종목명,수량,가상평단을 세미콜론으로 구분해야 합니다.')
        result[symbol]={'name':name,'quantity':quantity,'price':price}
    return result

def load_config(env_path=None):
    env_path = local_path(env_path or '.env')
    if not env_path.is_file():raise RMMTError('RMMT .env 파일이 필요합니다. .env.example을 참고하세요.')
    values = read_env(env_path)
    # The selected .env is authoritative. Process environment never overrides it.
    def get(key):
        if key not in values:raise RMMTError('필수 .env 설정 누락: '+key)
        return values[key]
    def boolean(key):
        value=get(key).lower()
        if value not in ('true','false'):raise RMMTError(key+': true 또는 false 필요')
        return value=='true'
    for key, expected in {'STRATEGY':'reverse',
                          'SKIP':'0',
                          'TIMEZONE':'Asia/Seoul',
                          'NO_TRADE_DAYS':'5','PERSISTENT_JUMP_PCT':'35','SHOCK_OBSERVATIONS':'5','LATE_START_POLICY':'WITHIN_WINDOW_ONLY'}.items():
        if get(key) != expected:
            raise RMMTError(f'{key}: 이번 버전은 {expected}만 지원합니다.')
    salt=get('BCSO_REFERENCE_ACCOUNT_SALT');reference_hash=get('BCSO_REFERENCE_ACCOUNT_HASH')
    cano=get('KIS_REAL_ACCOUNT').replace('-','')[:8]
    separate=bool(salt and reference_hash and len(cano)==8 and cano.isdigit() and not hmac.compare_digest(hmac.new(salt.encode(),cano.encode(),hashlib.sha256).hexdigest(),reference_hash))
    try:
        capital_manwon=Decimal(get('REVERSE_MOMENTUM_CAPITAL_MANWON'))
        if not capital_manwon.is_finite() or capital_manwon<=0 or capital_manwon!=capital_manwon.to_integral_value():
            raise RMMTError('REVERSE_MOMENTUM_CAPITAL_MANWON은 만원 단위 양의 정수여야 합니다.')
        formation_days=int(get('FORMATION_DAYS'))
        rebalance_days=int(get('REBALANCE_DAYS'))
        if not 5<=formation_days<=504:raise RMMTError('FORMATION_DAYS는 5~504 거래일 정수여야 합니다.')
        if not 1<=rebalance_days<=252:raise RMMTError('REBALANCE_DAYS는 1~252 거래일 정수여야 합니다.')
        from datetime import date
        anchor=get('REBALANCE_ANCHOR_DATE')
        if date.fromisoformat(anchor).isoformat()!=anchor:raise RMMTError('REBALANCE_ANCHOR_DATE는 YYYY-MM-DD 형식이어야 합니다.')
        market=get('MARKET').upper()
        if market not in ('KOSDAQ','KOSPI','COMBINED'):raise RMMTError('MARKET은 KOSDAQ, KOSPI, COMBINED 중 하나여야 합니다.')
        count = int(get('HOLDINGS'))
        decile = int(get('DECILE'))
        if not 0<=decile<=10:raise RMMTError('DECILE은 0~10 범위의 정수여야 합니다.')
        config = Config(preview_extra_holdings=values.get('PREVIEW_EXTRA_HOLDINGS',''),preview_holding_enabled=boolean('PREVIEW_HOLDING_ENABLED'),
            preview_holding_symbol=get('PREVIEW_HOLDING_SYMBOL'),preview_holding_name=get('PREVIEW_HOLDING_NAME'),
            preview_holding_quantity=int(get('PREVIEW_HOLDING_QUANTITY')),preview_holding_price=Decimal(get('PREVIEW_HOLDING_PRICE')),mode=get('RUN_MODE').upper(), environment=get('KIS_ENV'),
            capital=capital_manwon*Decimal('10000'),
            capital_policy=get('CAPITAL_POLICY'), holdings=count,decile=decile,market=market,formation_days=formation_days,rebalance_days=rebalance_days,rebalance_anchor_date=anchor,
            minimum_price=Decimal(get('MIN_PRICE')), buy_fee=Decimal(get('BUY_FEE')),
            sell_fee=Decimal(get('SELL_FEE')), sell_tax=Decimal(get('SELL_TAX')),
            order_policy=get('ORDER_POLICY').upper(),execution_time=get('REVERSE_EXECUTION_TIME'),
            **{field:boolean(key) for field,key in REGULATORY_SETTINGS.items()},
            window_minutes=int(get('EXECUTION_WINDOW_MINUTES')),
            shock_policy=get('SHOCK_POLICY'), dividend_policy=get('DIVIDEND_POLICY'),
            design_approved=boolean('DESIGN_APPROVED'),
            limit_policy=get('LIMIT_POLICY'), live_enabled=boolean('RMMT_LIVE_ENABLED'),
            account_policy=get('ACCOUNT_POLICY'),account_isolation_verified=separate,
            bcso_stop_confirmed=boolean('BCSO_STOP_CONFIRMED'),
            bcso_ownership_fix_confirmed=boolean('BCSO_OWNERSHIP_FIX_CONFIRMED'),
            rest_interval=float(get('KIS_REST_MIN_INTERVAL_SECONDS')),
            poll_seconds=float(get('POLL_SECONDS')), quote_max_age=int(get('QUOTE_MAX_AGE_SECONDS')),
            status_max_age=int(get('STATUS_MAX_AGE_SECONDS')),
            strategy_id=f'{market}_D{decile}_{formation_days}D_S0_{rebalance_days}D_N{count}',
            **{field:get(key) for field,key in PATH_SETTINGS.items()},
            dashboard_enabled=boolean('DASHBOARD_ENABLED'),auto_collect=boolean('SERVICE_AUTO_COLLECT'),data_ready_time=get('DATA_READY_TIME'),
            **{field:float(get(key)) for field,key in FLOAT_SETTINGS.items()},
            **{field:int(get(key)) for field,key in INT_SETTINGS.items()})
    except (ValueError, ArithmeticError):
        raise RMMTError('환경설정 숫자 형식이 잘못되었습니다.') from None
    preview_holdings(config)
    if config.preview_holding_quantity<=0 or not config.preview_holding_price.is_finite() or config.preview_holding_price<=0:
        raise RMMTError('가상 보유 수량과 평단은 양수여야 합니다.')
    if len(config.preview_holding_symbol)!=6 or not config.preview_holding_symbol.isdigit():raise RMMTError('가상 종목코드는 6자리 숫자여야 합니다.')
    if config.account_policy not in ('UNRESOLVED','SEPARATE','BCSO_STOPPED','SHARED_OWNED_ONLY','DEFERRED'):raise RMMTError('ACCOUNT_POLICY 값 오류')
    if config.mode not in ('DRY_RUN','PAPER','DEMO','LIVE') or config.environment not in ('real','demo'):
        raise RMMTError('RUN_MODE/KIS_ENV 값 오류')
    if count < 1 or not config.capital.is_finite() or config.capital <= 0:
        raise RMMTError('HOLDINGS는 양의 정수, 배정금액은 유한한 양수여야 합니다.')
    if config.capital_policy != 'FIXED_CAP' or config.order_policy not in ('MARKET','LIMIT'):
        raise RMMTError('자본/주문 정책 오류')
    if config.window_minutes != 30:
        raise RMMTError('현재 주문창 길이는 30분만 지원합니다.')
    from datetime import time as clock_time
    import re
    try:
        if not re.fullmatch(r'\d{2}:\d{2}',config.execution_time):raise ValueError()
        start=clock_time.fromisoformat(config.execution_time)
        minutes=start.hour*60+start.minute
        if minutes<9*60 or minutes+config.window_minutes>15*60+30:raise ValueError()
    except ValueError:
        raise RMMTError('REVERSE_EXECUTION_TIME은 HH:MM 형식의 09:00~15:00 시각이어야 합니다(주문창 30분).') from None
    if config.shock_policy not in ('UNRESOLVED','EXCLUDE_EXIT_REBALANCE') or config.dividend_policy not in ('UNRESOLVED','RETAIN'):
        raise RMMTError('급변/배당 정책 오류')
    if config.minimum_price <= 0 or not config.minimum_price.is_finite():
        raise RMMTError('MIN_PRICE는 양수 필요')
    for rate in (config.buy_fee, config.sell_fee, config.sell_tax):
        if not rate.is_finite() or not (0 <= rate < Decimal('.1')):
            raise RMMTError('비용 비율 오류')
    import math
    if any(not math.isfinite(v) or v<=0 for v in (config.rest_interval,config.poll_seconds,config.quote_max_age,config.status_max_age)):
        raise RMMTError('조회 간격/최신성 값 오류')
    for field in (*FLOAT_SETTINGS,*INT_SETTINGS):
        import math
        value=getattr(config,field)
        if not math.isfinite(value) or value<=0:raise RMMTError('양수 설정 필요: '+field)
    for field in PATH_SETTINGS:
        local_path(getattr(config,field))
    if len({local_path(getattr(config,k)) for k in ('system_log_path','trading_log_path','screen_log_path')})!=3:
        raise RMMTError('세 로그 경로는 서로 달라야 합니다.')
    try:
        from datetime import time
        import re
        if not re.fullmatch(r'\d{2}:\d{2}',config.data_ready_time):raise ValueError()
        time.fromisoformat(config.data_ready_time)
    except ValueError:raise RMMTError('DATA_READY_TIME은 HH:MM 형식 필요') from None
    if config.bootstrap_past_days+config.bootstrap_future_days>config.calendar_max_days:
        raise RMMTError('초기 달력 과거/미래 범위의 합은 CALENDAR_MAX_RANGE_DAYS 이하여야 합니다.')
    if config.bootstrap_past_days<60 or config.bootstrap_future_days<14:
        raise RMMTError('초기 달력은 과거60일/미래14일 이상 필요합니다.')
    if config.token_cooldown_seconds<60:raise RMMTError('토큰 발급 대기는 60초 이상 필요')
    credentials = {k: get(k) for k in ('KIS_REAL_APP_KEY','KIS_REAL_APP_SECRET','KIS_REAL_ACCOUNT',
        'KIS_DEMO_APP_KEY','KIS_DEMO_APP_SECRET','KIS_DEMO_ACCOUNT','KIS_ACCOUNT_PRODUCT_CODE')}
    return config, credentials
