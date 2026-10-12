"""Run outside the trading service: python collect_kis_history.py --output ..."""
import argparse,json,sys
from dataclasses import replace
from rmmt.config import load_config,RMMTError
from rmmt.kis import KIS
from rmmt.ledger import process_lock
from rmmt.history_bundle import HistoryBundle,HistoryCollector


class DataOnlyKIS(KIS):
    def request(self,method,path,*args,**kwargs):
        allowed={'/uapi/domestic-stock/v1/quotations/chk-holiday','/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice'}
        if method!='GET' or path not in allowed:raise RMMTError('수집 전용: 주문·취소·계좌 변경 API 금지')
        return super().request(method,path,*args,**kwargs)


def main(argv=None):
    p=argparse.ArgumentParser(description='전체 보통주 1년 KIS 수정 일봉·검증·스냅샷을 단일 SQLite 파일에 저장; 주문 없음')
    p.add_argument('--env',default='.env');p.add_argument('--output',default='rmmt/data/kis_history.sqlite')
    p.add_argument('--asof');p.add_argument('--token-cache',default='rmmt/data/.kis_token_history.json')
    args=p.parse_args(argv);c,credentials=load_config(args.env)
    c=replace(c,token_real_path=args.token_cache)
    def progress(value):print(json.dumps(value,ensure_ascii=False),flush=True)
    with process_lock(args.output+'.lock'):
        bundle=HistoryBundle(args.output)
        try:
            result=HistoryCollector(c,DataOnlyKIS(c,credentials),bundle,progress).run(args.asof)
            print(json.dumps({'file':args.output,'snapshot_ready':result['snapshot_ready'],'year_gap_count':result['year_gap_count']},ensure_ascii=False))
            return 0 if result['snapshot_ready'] else 2
        finally:bundle.close()

if __name__=='__main__':
    try:raise SystemExit(main())
    except RMMTError as exc:print('수집 오류: '+str(exc),file=sys.stderr);raise SystemExit(2)
