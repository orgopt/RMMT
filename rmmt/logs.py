"""Independent, redacted operational logs; the ledger remains authoritative."""
from collections import deque
import csv
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import local_path

_active = None
COLUMNS = ('event_key','mode','environment','ledger','id','intent_id','symbol','side','qty','gross','fee','tax','occurred_at','simulated','event_type')

STATUS_TEXT = {
    'WAITING_SELECTION':'다음 선정·리밸런싱 대기','PREPARING_SELECTION':'종목선정 중', 'SELECTION_READY':'종목선정 완료 — 리밸런싱 시작',
    'PREPARING_CALENDAR':'거래일 달력 수집 중', 'CALENDAR_READY':'거래일 달력 준비 완료',
    'PREPARING_MARKET_DATA':'종목·가격 자료 수집 중', 'PREPARING_SIGNAL_HISTORY':'종목별 과거 가격 수집 중',
    'MARKET_DATA_READY':'종목·가격 자료 수집 완료', 'OUTSIDE_WINDOW':'주문 시간 외 — 주문 대기',
    'WAITING_WINDOW':'오전 10시 주문 시작 대기', 'NON_SESSION':'휴장일 — 주문 없음',
    'WAITING_DATA_CLOSE':'장 마감 자료 수집 대기', 'AUTO_COLLECTION_DISABLED':'자동 자료 수집 꺼짐',
    'WEEKLY_SIGNAL_READY':'주간 종목 선정 완료', 'WAITING_WEEKLY_SIGNAL':'다음 주간 종목 선정 대기',
    'RECOVERY_REQUIRED':'복구 필요 — 오류 확인 후 재시도 대기', 'COMPLETE':'주문 처리 완료',
    'WINDOW_CLOSED':'주문 시간 종료', 'INTERRUPTED':'이전 주문 실행 중단 — 재실행 제한',
    'FAILED':'주문 처리 실패', 'DAY_ALREADY_EXECUTED':'오늘 이미 실행하여 추가 주문 없음',
}

def readable_event(message):
    """Summarize display events; retain full structured diagnostics in files."""
    text=str(message)
    if text.startswith('START '):
        mode=re.search(r'mode=(\w+)',text)
        return '프로그램 시작'+(' | 실행 모드: '+mode.group(1) if mode else '')
    if text=='STOP':return '프로그램 종료'
    if text.startswith('Traceback '):
        return '실행 오류: '+text.strip().splitlines()[-1]+' | 상세 내용은 system 로그 확인'
    try:value=json.loads(text)
    except (ValueError,TypeError):return ' '.join(text.split())
    if not isinstance(value,dict):return '운영 결과 기록 완료 | 상세 내용은 system 로그 확인'
    parts=[]
    for key in ('stage','state','preparation'):
        if value.get(key):parts.append(STATUS_TEXT.get(value[key],str(value[key])))
    if 'completed' in value and 'total' in value:
        parts.append(f"진행: {value['completed']}/{value['total']}건")
    if 'start' in value and 'end' in value:parts.append(f"조회 기간: {value['start']} ~ {value['end']}")
    if 'sessions' in value:parts.append(f"거래일: {value['sessions']}일")
    for key,label in [('day','기준일'),('date','자료 기준일'),('ready_after','자료 수집 시작'),
                      ('next_signal_date','다음 선정일'),('next_execution_date','다음 주문일'),('error','원인')]:
        if value.get(key):parts.append(label+': '+str(value[key]))
    if value.get('reason'):parts.append(STATUS_TEXT.get(value['reason'],str(value['reason'])))
    if value.get('new_rebalance_blocked_reason'):parts.append('신규 주문 차단: '+str(value['new_rebalance_blocked_reason']))
    return ' | '.join(parts) or '운영 결과 기록 완료 | 상세 내용은 system 로그 확인'

def diagnostic_error(operation, exc, **context):
    """Persist sanitized diagnostics without flooding the recent-event panel."""
    import traceback
    if _active:
        record={'operation':operation,'exception_type':type(exc).__name__,
            'exception_message':str(exc),'context':context,
            'traceback':''.join(traceback.format_exception(type(exc),exc,exc.__traceback__))}
        _active.write('system','DEBUG_ERROR '+json.dumps(record,ensure_ascii=False,default=str))

def realtime_event(message):
    """Only meaningful changes belong in the operator's recent-event panel."""
    text=str(message)
    if text.startswith('DEBUG_ERROR '):return False
    if text.startswith('START ') or text=='STOP':return False
    try:value=json.loads(text)
    except (ValueError,TypeError):
        return any(word in text.lower() for word in ('error','오류','실패','차단','체결','취소','중단','투자상한 변경'))
    if not isinstance(value,dict):return False
    if value.get('error') or value.get('new_rebalance_blocked_reason'):return True
    if value.get('stage'):return True
    return value.get('state') in ('RECOVERY_REQUIRED','COMPLETE','WINDOW_CLOSED','INTERRUPTED','FAILED')

class Logs:
    def __init__(self, config, secrets=()):
        self.config = config
        self.recent = deque(maxlen=config.dashboard_recent_lines)
        self.last_recent_kind = None
        self.secrets = sorted({str(v) for v in secrets if v and len(str(v)) >= 4}, key=len, reverse=True)
        self.handlers = {}
        for channel, field in [('system','system_log_path'),('screen','screen_log_path')]:
            path = local_path(getattr(config, field)); path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count, encoding='utf-8')
            path.chmod(0o600)
            handler.setFormatter(logging.Formatter('%(message)s'))
            self.handlers[channel] = handler
        path = local_path(config.trading_log_path); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a'): pass
        path.chmod(0o600)

    def redact(self, text):
        text = str(text)
        for value in self.secrets: text = text.replace(value, '[REDACTED]')
        text = re.sub(r'(?i)(Bearer\s+)[^\s"\']+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)(["\']?(?:access_token|appsecret|appkey|authorization|password)["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+', r'\1[REDACTED]', text)
        return text

    def write(self, channel, text):
        try:
            stamp = datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')
            if channel=='system' and realtime_event(text):
                compact=self.redact(readable_event(text))
                try:kind=json.loads(str(text)).get('stage')
                except (ValueError,AttributeError):kind=None
                # Replace the previous milestone of an ongoing collection.
                if kind and kind==self.last_recent_kind and self.recent:self.recent.pop()
                entry=stamp+' '+compact[:500]
                previous=self.recent[-1].split(' ',1)[-1] if self.recent else None
                if previous!=compact[:500]:self.recent.append(entry)
                self.last_recent_kind=kind
            record = logging.LogRecord('rmmt',logging.INFO,'',0,stamp+' '+self.redact(text),(),None)
            self.handlers[channel].handle(record)
            local_path(getattr(self.config,channel+'_log_path')).chmod(0o600)
        except Exception:
            # Logging failure must not convert an accepted broker order into a retry.
            print('RMMT 로그 기록 실패: 원장과 로그 저장 경로를 확인하세요.',file=sys.stderr)

    def sync(self, ledger):
        if ledger.db.in_transaction: return
        try:
            path = local_path(self.config.trading_log_path)
            with path.open('a+',encoding='utf-8',newline='') as file:
                fcntl.flock(file,fcntl.LOCK_EX)
                file.seek(0)
                reader = csv.DictReader(file)
                if reader.fieldnames and tuple(reader.fieldnames)!=COLUMNS:
                    raise ValueError('trading header mismatch')
                known = {r['event_key'] for r in reader}
                file.seek(0,2); writer = csv.DictWriter(file,fieldnames=COLUMNS)
                if file.tell()==0: writer.writeheader()
                source = str(ledger.path.relative_to(local_path('.')))
                for row in ledger.db.execute('SELECT * FROM fills ORDER BY rowid'):
                    item=dict(row)
                    key=hashlib.sha256((source+'\0'+item['id']).encode()).hexdigest()
                    if key in known: continue
                    item.update(event_key=key,mode=ledger.config.mode,environment=ledger.config.environment,
                        ledger=source,event_type='FILL' if item['qty'] else 'COST_RECONCILIATION')
                    writer.writerow({k:self.redact(item[k]) for k in COLUMNS});known.add(key)
                file.flush()
        except Exception:
            print('RMMT 체결 로그 기록 실패: 원장이 보존되어 다음 동기화에서 재시도합니다.',file=sys.stderr)

    def close(self):
        for handler in self.handlers.values(): handler.close()

def configure(config, secrets=()):
    global _active
    if _active: _active.close()
    _active=Logs(config,secrets)
    return _active

def current(): return _active

def event(message):
    if _active: _active.write('system',message)

def sync(ledger):
    if _active: _active.sync(ledger)
