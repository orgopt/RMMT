"""KIS REST adapter. No authentication until an explicit operation is requested."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib, json, os, time
import requests
from .config import RMMTError, local_path
from .ledger import process_lock
from .strategy import decimal, SEOUL

class BrokerRejected(RMMTError):
    pass
class BrokerUnknown(RMMTError):
    pass

class KIS:
    def __init__(self,config,credentials,session=None):
        self.config=config;self.session=session or requests.Session();self.last_request=0
        prefix='KIS_'+config.environment.upper()+'_'
        self.key=credentials.get(prefix+'APP_KEY','');self.secret=credentials.get(prefix+'APP_SECRET','')
        account=credentials.get(prefix+'ACCOUNT','').replace('-','')
        self.cano=account[:8];self.product=credentials.get('KIS_ACCOUNT_PRODUCT_CODE') or account[8:] or '01'
        if not self.key or not self.secret or len(self.cano)!=8 or not self.cano.isdigit() or len(self.product)!=2 or not self.product.isdigit():
            raise RMMTError('RMMT .env에 KIS 키/계좌(8-2)를 설정해야 합니다.')
        self.base='https://openapi.koreainvestment.com:9443' if config.environment=='real' else 'https://openapivts.koreainvestment.com:29443'
        self.cache=local_path(config.token_real_path if config.environment=='real' else config.token_demo_path)
        self.token=None;self.token_expires=0;self.token_issue_after=0
    def _pace(self):
        delay=self.config.rest_interval-(time.monotonic()-self.last_request)
        if delay>0:time.sleep(delay)
        self.last_request=time.monotonic()
    def authenticate(self):
        if time.time()<self.token_issue_after:raise RMMTError('KIS 토큰 재발급 대기 중: 최소 60초 간격을 유지합니다.')
        with process_lock(str(self.cache)+'.lock'):
            if self.cache.exists():
                try:
                    cache=json.loads(self.cache.read_text())
                    if cache.get('environment')==self.config.environment and cache.get('app_key_hash')==hashlib.sha256(self.key.encode()).hexdigest() and cache.get('expires_epoch',0)>time.time()+self.config.token_margin_seconds:
                        self.token=cache['access_token'];self.token_expires=cache['expires_epoch'];return
                except (ValueError,KeyError):pass
            self.token_issue_after=time.time()+self.config.token_cooldown_seconds
            self._pace()
            try:
                response=self.session.post(self.base+'/oauth2/tokenP',json={'grant_type':'client_credentials','appkey':self.key,'appsecret':self.secret},timeout=(self.config.connect_timeout,self.config.request_timeout))
                payload=response.json()
            except (requests.RequestException,ValueError):
                raise RMMTError('KIS 인증 응답 불명확: 자동 재발급하지 않습니다.') from None
            if response.status_code!=200 or not payload.get('access_token'):
                raise RMMTError('KIS 인증 실패: 키·환경·발급 제한 확인 필요')
            cache={'environment':self.config.environment,'app_key_hash':hashlib.sha256(self.key.encode()).hexdigest(),
                   'access_token':payload['access_token'],'expires_epoch':time.time()+int(payload.get('expires_in',0)),
                   'created_at':datetime.now(timezone.utc).isoformat()}
            self.cache.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.cache.with_suffix('.tmp')
            fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as handle:json.dump(cache,handle)
            os.replace(tmp,self.cache);os.chmod(self.cache,0o600)
            self.token=cache['access_token'];self.token_expires=cache['expires_epoch']
    def request(self,method,path,tr_id,params,continuation=''):
        if self.token is None or (self.token_expires and self.token_expires<=time.time()+self.config.token_margin_seconds):self.authenticate()
        headers={'content-type':'application/json; charset=utf-8','authorization':'Bearer '+self.token,
                 'appkey':self.key,'appsecret':self.secret,'tr_id':tr_id,'custtype':'P','tr_cont':continuation}
        if method=='POST':
            # A hash-key timeout precedes order submission; the caller conservatively records UNKNOWN.
            self._pace()
            try:
                response=self.session.post(self.base+'/uapi/hashkey',headers={'content-type':'application/json','appkey':self.key,'appsecret':self.secret},json=params,timeout=(self.config.connect_timeout,self.config.hash_timeout))
                response.raise_for_status();headers['hashkey']=response.json()['HASH']
            except (requests.RequestException,ValueError,KeyError):
                raise BrokerUnknown('KIS hashkey 조회 실패; 접수 대조 후 처리 필요') from None
        self._pace()
        try:
            response=self.session.request(method,self.base+path,headers=headers,
                params=params if method=='GET' else None,json=params if method=='POST' else None,timeout=(self.config.connect_timeout,self.config.request_timeout))
            payload=response.json()
        except (requests.RequestException,ValueError) as exc:
            from .logs import diagnostic_error
            diagnostic_error('kis_request',exc,method=method,path=path,tr_id=tr_id)
            error=BrokerUnknown if method=='POST' else RMMTError
            raise error('KIS 응답 불명확: 주문은 자동 재전송하지 않습니다.') from None
        if not isinstance(payload,dict):
            error=BrokerUnknown if method=='POST' else RMMTError
            raise error('KIS 응답 형식 오류: 조회 재시도 또는 주문 접수 대조가 필요합니다.')
        if response.status_code!=200:
            from .logs import diagnostic_error
            diagnostic_error('kis_http_response',RMMTError('KIS HTTP '+str(response.status_code)),method=method,path=path,tr_id=tr_id,http_status=response.status_code,msg_cd=payload.get('msg_cd'),msg1=payload.get('msg1'))
            error=BrokerUnknown if method=='POST' else RMMTError
            raise error(f'KIS HTTP {response.status_code}: 민감한 원문은 출력하지 않습니다.')
        if str(payload.get('rt_cd'))!='0':
            code=str(payload.get('msg_cd','UNKNOWN'))
            from .logs import diagnostic_error
            diagnostic_error('kis_api_rejection',RMMTError('KIS 거절 코드: '+code),method=method,path=path,tr_id=tr_id,http_status=response.status_code,rt_cd=payload.get('rt_cd'),msg_cd=code,msg1=payload.get('msg1'))
            # Even explicit API failures can be throttling/transient; do not blindly retry POST.
            raise BrokerRejected('KIS 거절 코드: '+code)
        return payload,response.headers
    def account(self):return {'CANO':self.cano,'ACNT_PRDT_CD':self.product}
    def tr(self,suffix):return ('T' if self.config.environment=='real' else 'V')+suffix
    def pages(self,path,tr_id,params,field='output1'):
        params=dict(params);params.update(CTX_AREA_FK100='',CTX_AREA_NK100='')
        rows=[];continuation='';seen=set();last=None
        for _ in range(self.config.account_max_pages):
            body,headers=self.request('GET',path,tr_id,params,continuation)
            current=body.get(field)
            if not isinstance(current,list):raise RMMTError('KIS 조회 목록 계약 변경')
            rows.extend(current);last=body
            if headers.get('tr_cont','') not in ('M','F'):
                return rows,last
            cursor=(body.get('ctx_area_fk100',''),body.get('ctx_area_nk100',''))
            if cursor in seen or not any(cursor):raise RMMTError('KIS 연속조회 커서 오류: 불완전 결과 사용 금지')
            seen.add(cursor);params.update(CTX_AREA_FK100=cursor[0],CTX_AREA_NK100=cursor[1]);continuation='N'
        raise RMMTError('KIS 연속조회 설정 최대 페이지 초과')
    def quote(self,symbol):
        body,_=self.request('GET','/uapi/domestic-stock/v1/quotations/inquire-price','FHKST01010100',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':symbol})
        return body['output']
    def balance(self):
        params=self.account()|{'AFHR_FLPR_YN':'N','OFL_YN':'','INQR_DVSN':'02','UNPR_DVSN':'01',
           'FUND_STTL_ICLD_YN':'N','FNCG_AMT_AUTO_RDPT_YN':'N','PRCS_DVSN':'00'}
        rows,body=self.pages('/uapi/domestic-stock/v1/trading/inquire-balance',self.tr('TTC8434R'),params)
        holdings={}
        for r in rows:
            if int(r['hldg_qty'])>0:
                if r['pdno'] in holdings:raise RMMTError('잔고 종목 중복: 합산 계약 확인 필요')
                holdings[r['pdno']]={'quantity':int(r['hldg_qty']),'sellable':int(r['ord_psbl_qty'])}
        return holdings,body.get('output2')
    def buy_power(self,symbol):
        body,_=self.request('GET','/uapi/domestic-stock/v1/trading/inquire-psbl-order',self.tr('TTC8908R'),
            self.account()|{'PDNO':symbol,'ORD_UNPR':'0','ORD_DVSN':'01','CMA_EVLU_AMT_ICLD_YN':'Y','OVRS_ICLD_YN':'N'})
        output=body['output']
        if 'nrcvb_buy_amt' not in output or 'nrcvb_buy_qty' not in output:raise RMMTError('미수없는 매수가능 필드 없음')
        return decimal(output['nrcvb_buy_amt']),int(output['nrcvb_buy_qty'])
    def daily_orders(self,day):
        compact=day.replace('-','')
        rows,_=self.pages('/uapi/domestic-stock/v1/trading/inquire-daily-ccld',self.tr('TTC0081R'),
            self.account()|{'INQR_STRT_DT':compact,'INQR_END_DT':compact,'SLL_BUY_DVSN_CD':'00','PDNO':'','CCLD_DVSN':'00',
            'INQR_DVSN':'01','INQR_DVSN_3':'00','ORD_GNO_BRNO':'','ODNO':'','INQR_DVSN_1':'','EXCG_ID_DVSN_CD':'ALL'})
        return rows
    def cancellable(self):
        if self.config.environment!='real':
            raise RMMTError('KIS 모의 취소가능조회 공식 계약 미확인: PAPER를 사용하세요.')
        rows,_=self.pages('/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl','TTTC0084R',
            self.account()|{'INQR_DVSN_1':'0','INQR_DVSN_2':'0'},'output')
        return rows
    def submit(self,order):
        self.config.validate_live()
        params=self.account()|{'PDNO':order['symbol'],'ORD_DVSN':'01' if self.config.order_policy=='MARKET' else '00',
            'ORD_QTY':str(order['quantity']),'ORD_UNPR':'0' if self.config.order_policy=='MARKET' else str(int(Decimal(order['price_bound']))),
            'EXCG_ID_DVSN_CD':'KRX','SLL_TYPE':'01' if order['side']=='SELL' else '','CNDT_PRIC':''}
        body,_=self.request('POST','/uapi/domestic-stock/v1/trading/order-cash',self.tr('TTC0012U' if order['side']=='BUY' else 'TTC0011U'),params)
        output=body.get('output',{})
        if not output.get('ODNO') or not output.get('KRX_FWDG_ORD_ORGNO'):raise BrokerUnknown('KIS 접수번호/지점번호 누락')
        return str(output['ODNO']),str(output['KRX_FWDG_ORD_ORGNO'])
    def cancel(self,order):
        self.config.validate_live()
        rows=self.cancellable();match=[r for r in rows if str(r.get('odno',''))==order['broker_id'] and str(r.get('ord_gno_brno',''))==order['branch']]
        if len(match)!=1 or int(match[0]['psbl_qty'])<=0:
            raise RMMTError('취소가능 잔량을 확인하지 못했습니다. 주문조회로 복구 필요')
        body,_=self.request('POST','/uapi/domestic-stock/v1/trading/order-rvsecncl',self.tr('TTC0013U'),
            self.account()|{'KRX_FWDG_ORD_ORGNO':order['branch'],'ORGN_ODNO':order['broker_id'],
            'ORD_DVSN':'01' if self.config.order_policy=='MARKET' else '00','RVSE_CNCL_DVSN_CD':'02',
            'ORD_QTY':str(int(match[0]['psbl_qty'])),'ORD_UNPR':'0','QTY_ALL_ORD_YN':'Y','EXCG_ID_DVSN_CD':'KRX'})
        return body
