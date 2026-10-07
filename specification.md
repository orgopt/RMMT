# RMMT 프로젝트 명세

최종 갱신: 2026-10-07 (사용자 요청 및 현재 소스 기준)

이 문서는 Reverse_Momentum 프로젝트의 현재 구현과 운영 계약을 설명한다. 과거 대화 없이도 개발자가 프로그램을 이해하고 수정할 수 있도록 작성한다. 운영 규칙은 현재 `main_rmmt.py`, `rmmt/`, `.env.example`을 기준으로 하며 이전 PDF·연구 결과의 규칙과 구분한다. 실제 사용자 설정과 비밀값의 유일한 원본은 `.env`이다. 이 문서에 앱키·시크릿·계좌번호·비밀번호·토큰을 기록하지 않는다.

## 1. 목적과 프로젝트 영역

RMMT는 시가총액 집단 안에서 과거 수익률이 낮은 보통주를 선정하여 균등 배분하는 역모멘텀 전략이다. KIS 실계좌 주문을 중심으로 독립적인 현금·보유수량·주문·체결 원장을 유지한다. 다른 전략이 같은 계좌를 사용해도 RMMT가 매수하여 귀속시킨 수량만 매도한다. 계좌 전체 잔고를 RMMT 소유로 가져오지 않는다. 공유계좌 현금 배분은 사용자가 조정한다.

프로젝트는 실전 운영과 연구용 도구를 포함한다. `Etc/src/`, `Etc/Reference/`, `Etc/output/`, `dashboard/`의 과거 백테스트와 웹 대시보드는 실전 터미널 서비스와 다른 실행 경로다. 연구 성과를 현재 실전 규칙의 검증된 성과로 간주하지 않는다. 과거 경고 지정 이력·상장폐지·권리변동 자료의 누락 및 대리변수 한계는 README와 각 연구 보고서를 참조한다.

## 2. 파일과 모듈 책임

| 위치 | 책임 |
|---|---|
| `main_rmmt.py` | CLI, 시작 시각, 서비스 루프, 준비·주문·조회 명령 연결 |
| `rmmt/config.py` | 환경설정 파싱·검증, 전략 ID·설정 해시, 경로 제한, 주문 활성화 검사 |
| `rmmt/strategy.py` | 스냅샷 검증, 십분위, 포메이션, 급변 판정, 후보 순위, 목표수량·일정 |
| `rmmt/bootstrap.py` | 공식 달력 확보, 마감 수집, 당일 선정 준비, 중복 준비 방지 |
| `rmmt/data.py` | JSON 입출력, 공식 종목 마스터, Yahoo 수정가격, 달력, 일별 자료, 현재가 캐시 |
| `rmmt/market.py` | KIS 시총·명목 일봉·이력 캐시·경고 상태 해석 |
| `rmmt/kis.py` | KIS 인증, 요청 속도 제한, 연속조회, 잔고·매수가능금액·주문·취소 |
| `rmmt/execution.py` | 매도 선행, 매수 후행, 체결 대조, 잔량 취소, UNKNOWN 복구, 자체 모의체결 |
| `rmmt/ledger.py` | SQLite 원장, 거래·현금·수량·평가 사건, 실행 잠금, 귀속수량 및 예약금 |
| `rmmt/logs.py` | 비밀값 마스킹, system/trading/screen 로그 및 중요 알림 요약 |
| `rmmt/dashboard.py` | 읽기 전용 터미널 화면, 일정·선정 요약·보유·손익·가상 보유 |
| `rmmt/reporting.py` | 실행별 JSON/CSV 보고서 |
| `Etc/tests/` | 전략·주문·복구·수집·설정·화면·로그의 자동 검증 |
| `.env` / `.env.example` | 실제 설정 / 비밀값 없는 설정 양식 |
| `Etc/docs/` | 기존 개발 상세 명세, 운영 설명, KIS 계약, 검증 및 예시 자료 |
| `specification.md` | 프로젝트 전체의 최신 통합 설명 (본 문서) |
| `AGENTS.md` | 변경 승인 및 문서 동시 갱신 지침 |

BCSO/nightGap 구현을 참고했지만 RMMT 런타임이 해당 전략 폴더의 소스·설정·토큰 파일을 직접 가져와 사용하지 않는다. 별도 명시 승인이 없는 한 참조 폴더를 변경하지 않는다.

## 3. 전략 정의

### 3.1 시장 및 시총 집단

`MARKET`은 KOSDAQ, KOSPI, COMBINED 중 선택하며 대소문자를 정규화한다. COMBINED는 두 시장 보통주를 합친 하나의 모집단에서 십분위를 구한다. 공식 마스터로 보통주를 판별하며 우선주 등은 제외한다.

가격·거래·경고 필터를 적용하기 **전에** 모집단 전체를 `(시총 오름차순, 종목코드 오름차순)`으로 정렬한다. `DECILE=0`은 전체, 1은 최소 시총, 10은 최대 시총 집단이다. n개를 10개 집단으로 나누되 q=n//10, r=n%10으로 앞 r개 집단은 q+1개, 나머지는 q개를 배정한다. 시총 동률은 코드순이다.

### 3.2 포메이션과 순위

`FORMATION_DAYS=F`는 5~504 **시장 거래일**이다. 주문일 당일 가격을 포메이션에 넣지 않고 직전 완료 거래일을 신호 기준일로 사용한다. 기준일 및 F거래일 전 가격을 포함하는 F+1개 종가 자료가 필요하다.

`formation_return = split_adjusted_close[기준일] / split_adjusted_close[F거래일 전] - 1`

가격은 분할만 조정한 가격이며 현금 배당 재투자를 신호 가격에 섞지 않는다. 현재 수집 구현은 Yahoo 가격·분할 자료와 KIS 명목 가격의 검증을 사용한다. KIS만 사용하는 단일 공급원 구현이 아니다. 수익률 오름차순, 동률 종목코드순으로 역모멘텀 순위를 정한다. 화면의 역모멘텀 수치는 이 수익률의 퍼센트 표현이며 별도의 점수 변환은 없다.

### 3.3 적격 필터

- 가격 기준일 명목종가가 `MIN_PRICE` 이상이어야 한다.
- 포메이션 가격이 모두 양수이며 수정 기준·자료 출처가 검증되어야 한다.
- 최근 5거래일 거래량이 확인되고 기준일 거래량이 양수여야 한다. 최근 5일 모두 0인 경우도 제외한다.
- 신호일 거래 가능 여부와 급변 이력의 완전성을 확인한다.
- 관리, 투자주의환기, 투자주의, 투자경고, 투자위험 중 `.env`에서 제외하도록 설정한 지정 종목을 제외한다.
- 경고 자료가 없거나 시점·유효기간·출처를 확인할 수 없으면 정상 종목으로 추정하지 않는다. 개별 경고 필터가 false여도 미확인 자료의 매수 차단은 유지된다.
- 실행 시 후보를 현재 시세·현재 경고로 다시 검증하며 원래 순위의 차순위 적격 후보로 대체한다. 경고가 거래 중에도 변할 수 있으므로 신호 생성 때의 검사만으로 주문하지 않는다.

목표 수 `HOLDINGS`는 양의 정수이며 20개 상한은 없다. 적격 후보가 적으면 실제 선정된 개수로 균등 배분한다. 신규 후보가 없어도 보유·기존 주문 처리 규칙은 별도로 적용한다.

### 3.4 급변 및 무거래

직전 유효 실제 거래가격 대비 절대 변동률이 35%를 **초과**하면 PENDING_JUMP로 신규 매수를 보류한다. 이후 유효 실제 거래 관측 5회를 검사한다. 급변 직전 가격의 +35% 또는 -35% 경계 밖에 계속 있으면 CONFIRMED_PERSISTENT로 확정한다. 중간에 경계 안으로 돌아오면 NORMAL로 복귀한다. 지속 급변 확정은 이력 재생상 제외를 유지하고 기존 보유는 다음 리밸런싱의 청산 대상으로 처리한다. 미래 가격으로 과거 선정을 바꾸지 않는다.

보유분이 연속 5시장거래일 무거래이면 평가가를 0으로 기록하고 실제 수량을 보존하며 `exit_required`로 표시한다. 실제 거래가 재개되면 매 거래일 설정 주문창에서 회복 매도를 시도한다. 0원 평가는 실제 매도나 수량 삭제를 의미하지 않는다.

### 3.5 자금과 목표수량

투자상한은 만원 단위 환경설정 × 10,000원이다. 기본 양식 1000은 1,000만원이다. 수익이 나도 상한을 유지하고 손실이 나면 실제 전략자산 범위 내에서 투자한다. 확인된 전략 귀속 배당은 전략 현금에 남긴다.

`전략자산 = 전략현금 + 귀속 보유 평가액`

`배분가능금액 = max(0, min(전략자산, 투자상한) - 매도 불가능한 동결자산 평가액)`

`종목별 목표금액 = 배분가능금액 / 실제 선정 종목 수`

`목표수량 = floor(목표금액 / (현재가 × (1 + 매수수수료율)))`

목표수량과 보유수량의 차이를 주문한다. 유지 종목은 전량 매도 후 재매수가 아니라 필요 차이만 조정한다. 소수점 주식은 주문하지 않으며 잔액은 현금이다. 실매수량은 예약금, 보유 투자액, 계좌 매수 가능금액, 수수료 및 가격 경계로 추가 제한한다. 실매도량은 RMMT 귀속 매도가능수량과 증권사 매도가능수량 중 작은 값이다.

## 4. 거래일 일정 및 서비스 흐름

일정은 KST 기준이다. `REBALANCE_ANCHOR_DATE` 이상인 첫 공식 거래일부터 `REBALANCE_DAYS` 거래일마다 선정·리밸런싱한다. 매일=1, 1주≈5, 1개월≈21, 1년≈252 거래일이며 실제 달력 월·연 단위와 같지는 않다. 월요일 휴장이면 기준일 이후 첫 개장일로 순연된다. 재시작해도 기준일을 새로 잡지 않는다.

`REVERSE_EXECUTION_TIME`은 09:00~15:00 HH:MM이며 현재 주문창 길이는 30분이다. 예: 10:00에 선정 시작 → 선정 완료 직후 매도 → 매수 → 10:30 주문창 종료. 별도 금요일 오후 선정 규칙은 현재 사용하지 않는다. 선정·검증에 걸린 시간도 주문창에 포함한다.

휴장은 KIS 공식 `chk-holiday` 조회의 해당 날짜 및 `opnd_yn`으로 판단한다. 토요일·일요일만 빼거나 임의 공휴일 목록으로 추정하지 않는다. 공식 달력 JSON을 캐시하며 필요 범위가 부족하거나 무효하면 재확보한다. 유효 캐시를 재사용하므로 임시 휴장 변경을 계속 실시간 감지하는 구현은 아니다.

1. 설정 검증, 모드별 원장 및 잠금 확보, 로그·대시보드 준비.
2. 이전 주문·체결을 대조하고 종료된 주문창의 잔량 취소를 처리한다.
3. 공식 달력을 확보하고 오늘의 거래일·주문창·다음 일정을 판단한다.
4. 설정 주문창 안의 예정일이면 직전 거래일에 저장한 공식 스냅샷으로 선정한다. 같은 실행일·동일 설정으로 이미 준비한 자료는 재사용한다.
5. 현재 경고·가격·거래 가능 여부로 후보를 재검증하고 실행 계획을 저장한다.
6. 비선정 기존 종목 및 유지 종목 초과수량을 매도한다. 매도 미완료가 있으면 매수 단계로 무리하게 넘어가지 않는다.
7. 실제 매도 후 현금·보유량으로 매수 계획을 다시 산정한다. 부족한 후보는 동일 신호의 차순위로 대체한다.
8. 주문창 끝이면 미체결 잔량 취소를 요청한다. 취소 확인 전에는 완료로 가정하지 않는다. 같은 날 실행을 중복 시작하지 않는다.
9. 매 거래일 `DATA_READY_TIME` 이후 자동 수집이 켜져 있으면 마감 자료를 수집·검증·저장하고 보유 평가 및 준비 상태를 기록한다.
10. 대기 중 화면을 갱신하고 보유·선정·가상 보유 시세를 REST로 조회한다. 오류 발생 시 기록하고 설정 대기 후 다시 점검한다.

직전 거래일 공식 자료가 없으면 과거의 종목 마스터·시총을 오늘 자료로 재구성해 매매하지 않는다. 준비 실패로 신규 리밸런싱을 차단하되 회복 매도는 별도 검증을 통과해야 한다. 오래 걸리는 최초 달력·이력 수집은 진행 알림을 표시한다.

## 5. 주문 및 모드 계약

| 모드 | 의미 |
|---|---|
| DRY_RUN | 설정·신호·계획 확인용, 브로커 주문 없음 |
| PAPER | 자체 모의체결 및 독립 paper 원장, KIS 체결이 아님 |
| LIVE | KIS real 주문, 활성화 및 계좌 병행 조건 충족 필요 |
| DEMO | 설정·원장 경로는 있으나 현재 주문 실행 지원을 완성하지 않아 차단 |

인자 없는 `python main_rmmt.py`는 **service를 시작하며 실행 모드는 `.env`에서 읽는다**. 무조건 LIVE로 하드코딩하지 않는다. 사용자 운영 `.env`를 LIVE로 구성하면 인자 없이 실전 서비스가 실행된다. `.env.example`은 DRY_RUN과 실거래 비활성화 양식이다.

MARKET은 시장가, LIMIT은 최초 현재가 지정 후 가격 추격 없이 잔량 취소하는 CURRENT_NO_CHASE 정책이다. 시세·경고의 유효시간 및 주문창을 주문 전에 재확인한다. 주문 제출 전 의도(intent)를 저장하고 SUBMITTING으로 전환한다. 응답을 잃어 접수 여부가 불명확하면 UNKNOWN으로 두며 자동 재주문하지 않는다.

주문 상태의 주요 값: PLANNED, SUBMITTING, ACKNOWLEDGED, PARTIALLY_FILLED, CANCEL_PENDING, UNKNOWN, FILLED, CANCELLED, REJECTED. 완료 상태는 FILLED/CANCELLED/REJECTED이다. 증권사 누적 체결수량·금액을 대조하여 새 차이만 반영하므로 같은 조회를 반복해도 보유량을 중복 증가시키지 않는다. 비용 확정 뒤 수수료·세금 차액은 별도 정정 사건으로 남긴다.

계좌 병행 정책은 SEPARATE / BCSO_STOPPED / SHARED_OWNED_ONLY이다. LIVE는 정책별 확인과 설계 승인·급변/배당 정책 확정을 요구한다. 같은 계좌의 타 전략 미체결 주문이 발견되면 현재 구현은 실행을 차단할 수 있다. 자기 수량만 매도한다고 해서 공유 현금·다른 전략 주문·API 한도 간섭까지 제거되는 것은 아니다.

## 6. 현재가 및 데이터 계약

현재 RMMT는 웹소켓을 연결하지 않는다. 화면 현재가는 KIS REST 주기 조회이고 `DASHBOARD_QUOTE_REFRESH_SECONDS`는 최소 조회 간격이다. 종목 수·요청 간격·응답 지연에 따라 실제 갱신은 더 느릴 수 있다. 표시용 조회는 기존 경고 조회시각을 새로 만들지 않는다. 주문 판단용 가격·경고는 별도의 유효성 검사를 통과해야 한다.

신선한 시세이면 화면 현재금액(1주 현재가)과 평가금액(현재가×수량)을 갱신한다. 시세가 만료되면 원장 평가가격으로 되돌아가고 자료 시점을 표시한다. `exit_required` 보유는 0원 평가를 유지한다. 읽기 전용 dashboard 명령은 로컬 저장 자료만 읽으며 서비스처럼 KIS 시세를 계속 수집하지 않는다.

스냅샷에는 신호일, 실행일, cutoff, 시장, 거래일 배열, 전체 보통주와 시총 기준일, 명목·분할조정 가격, 거래량, 경고 출처·시점·유효기간, 급변 이력 완전성, 자료 계보가 필요하다. 선정 후보는 symbol, formation_return, cap_rank, rank, selected를 갖는다. 당일 선정 메타데이터에는 selected_at, selection_date, config_hash, selection_positions를 저장한다. 상세 필드 검증은 strategy.validate_snapshot와 Etc/docs/KIS_API_CONTRACT.md를 따른다.

## 7. 터미널 대시보드

상단은 전략이름, 프로그램 시작시간, 업데이트, KIS 접속 환경, 선정 요약, 다음 두 일정 순이다. 실행상태 및 자료상태 독립 라인은 표시하지 않는다. 주문 차단 사유는 별도로 표시한다. 선정 요약은 `20종목 선정완료(평균 … / 중위 …)` 형식이다. 다음 일정은 예정 시각과 선정 완료 직후 리밸런싱 관계를 보여준다.

요약은 전략현금·주문예약금·가용현금, 투자상한, 누적손익, 최근거래를 표시한다. 누적손익은 전략자산에서 외부 현금흐름을 차감한 값이며 가상 보유를 제외한다. 최근거래는 최근 실제 매도 주문의 체결 손익·비용 기준 수익률·날짜이며 수수료 정정과 권리변동 이력을 반영한다.

테이블 컬럼 순서:

`종목코드 / 종목명 / 선정 / 경고 / 수량 / 평단 / 매수금액 / 현재금액 / 평가금액 / 매도금액 / 평가손익(%) / 상태`

세로 구분선은 없다. 종목명은 고정 **20터미널 칸**(한글 2칸, 영문 1칸)이며 초과분은 말줄임한다. 종목명과 상태 제목·값은 왼쪽, 나머지 제목과 숫자는 오른쪽 정렬이다. 나머지 폭은 현재 표시할 제목·데이터·합계의 최대 실제 폭을 사용한다. 전체 화면 폭을 넘어갈 때 자동으로 컬럼을 재구성하는 구현은 아니다.

평가손익은 `12,500 (0.5)`처럼 금액과 수익률을 함께 표시한다. %는 컬럼명에만 있고 값에는 없으며 양수 +는 생략한다. 음수 -는 유지하고 수익률은 소수점 한 자리다. 종목이 없어도 컬럼명과 구분선은 유지한다. 마지막 행 아래 구분선과 합계 행에 매수금액·평가금액·평가손익을 표시한다. 합계 수익률은 총 손익/총 매수금액이며 가상 보유도 **화면 합계**에는 포함한다.

선정 구분: 기존=기존 보유지만 비선정, 신규=미보유에서 선정, 유지=보유하면서 재선정. 선정 당시 실제 보유 스냅샷으로 구분하므로 매수 후 신규가 유지로 바뀌지 않는다. 단계 중 상태는 주문·체결 상태를 표시하고 리밸런싱 COMPLETE 뒤 정상 보유는 Holding이다. 청산 완료된 기존 행은 COMPLETE 후 삭제하고 미완료·회복 매도 대상은 남긴다.

가상 보유는 PREVIEW 설정으로 표시하며 원장·예약금·주문·누적손익에 반영하지 않는다. 실제 같은 종목 보유가 있으면 가상 보유로 덮어쓰지 않는다. 가상 보유 상태는 `가상보유 / 매매대상 아님`이다. 기본 삼성전자와 추가 9종목은 예시 평단이며 실제 잔고가 아니다. 전체 제거는 PREVIEW_HOLDING_ENABLED=false, 추가분만 제거는 PREVIEW_EXTRA_HOLDINGS를 비운다.

최근 운영 로그 제목 오른쪽 공백 세 칸 뒤 현재 KST 날짜·시간을 `YYYY.MM.DD HH:MM:SS KST`로 표시한다. 터미널(TTY)에서는 시간만 하늘색 ANSI 색상이며 파일·일반 출력에는 색상 코드를 넣지 않는다. 최근 패널은 현재 실행의 중요한 알림만 표시하며 과거 파일 전체를 다시 읽지 않는다. 반복 대기는 생략하고 수집 진행은 최신 단계로 축약한다. 실행 후보 검증과 submit의 초기 필터 차단은 코드·매수/매도·한글 사유를 표시한다. 예: `005930 매수 차단: 투자경고종목`. 모든 최종 재확인 분기 및 모든 신호일 제외종목을 개별 최근 알림으로 내보내는 것은 아직 아니다. 상세 결정은 스냅샷·보고서·감사 원장을 확인한다.

## 8. 로그와 상태 저장

| 저장소 | 내용과 동작 |
|---|---|
| system_rmmt.log | 실행·수집·상태·오류·차단 상세 기록, 비밀값 마스킹 |
| trading_rmmt.log | CSV 형식 확정 체결·비용 정정, 사건키 기반 중복 방지, 전체 이력 유지 |
| screen_rmmt.log | 화면 및 JSON 진단, 콘솔에서 생략한 상세 자료도 기록 가능 |
| calendar.json | 공식 거래일 및 수집 범위·출처 |
| snapshots/YYYY-MM-DD.json | 해당 날짜 수집 당시 공식 자료, 다음 거래일 신호용 |
| signal_latest.json | 선정 입력과 당일 선정 메타데이터 |
| daily_latest.json | 보유 평가에 쓰는 최신 일별 자료 |
| preparation_state.json | 수집 완료 및 설정·보유 관련 재시작 준비 상태 |
| KIS 토큰 캐시 | 환경별 독립 접속 토큰, 0600 권한 |
| 모드별 SQLite | 현금·보유·주문·체결·실행·감사·평가의 정본 |
| rmmt/results | 실행별 신호·계획·결과 보고서 |

system/screen 로그는 기본 10MiB마다 회전하고 이전 5개를 보관한다. trading은 같은 회전 정책으로 잘라내지 않는다. 비밀값은 출력 전 마스킹하며 관련 파일 권한을 제한한다. .env 로드가 프로세스 환경변수보다 우선하며 변경 후 재시작해야 한다. 설정을 바꾼 상태로 예전 당일 신호를 그대로 주문하지 않도록 설정 해시를 검사한다.

SQLite는 WAL 및 foreign_keys를 사용한다. 금액은 Decimal을 사용하고 DB에는 문자열로 저장한다. 주요 테이블:

- meta: 현금, 초기자산, 투자상한, 모드 등.
- positions: 귀속수량, 잔여 취득원가, mark, exit_required, valuation_status.
- runs: 날짜·종류별 유일 실행, config_hash, 상태, 선정 당시 보유량 등 payload.
- orders: 주문 의도, 방향·수량·가격경계·상태·증권사번호·지점·누적체결·예약금.
- fills: 중복되지 않는 체결 사건 및 비용 정정.
- cash_events: 초기 배정·배당·복구·수수료 등 현금 사건과 외부흐름 여부.
- quantity_events: 권리변동 수량 사건 및 근거.
- valuation_events: 0원 평가 등 가격 변경 이유와 수량.
- nav: 날짜별 현금·평가·전략자산·정규화 자산.
- audit: 실행 계획, 후보 대체, 필터 차단, 취소 미확인 등 상세 감사.

모드별 DB를 혼용하지 않으며 프로세스는 원장 경로별 flock으로 중복 사용을 막는다. 같은 날짜·실행 종류 UNIQUE 제약과 의도 저장으로 재시작 중복 주문을 차단한다. 전략 귀속수량보다 계좌 실제 잔고가 적으면 실행을 차단하여 수동 거래·타 전략 간섭을 먼저 확인한다.

### 8.1 상세 오류 진단

system 로그의 `DEBUG_ERROR` JSON에는 operation, exception_type, exception_message, context, traceback을 기록한다. 현재가 실패는 종목코드·이전 캐시 가격·시각을, KIS 요청 실패는 HTTP 메서드·API 경로·TR ID를 기록한다. HTTP/API 거절은 HTTP 상태, rt_cd, msg_cd, msg1을 추가한다. 인증 헤더·요청 본문·계좌 파라미터 및 전체 원문 응답은 기록하지 않으며 기존 비밀값 마스킹을 적용한다. 상세 진단은 최근 패널에서 제외하고 기존의 짧은 실패 알림을 유지한다. 조회 재시도·주문 재전송 정책은 바꾸지 않는다. 변경 이전의 오류 원인은 소급 복구할 수 없다.

## 9. 실행 명령과 복구

Python 3 및 requirements-rmmt.txt의 requests가 필요하다. 금융 계산과 원장은 표준 라이브러리를 사용한다. 명령은 프로젝트 루트에서 실행한다. `--env`는 서브명령 앞에 지정하며 파일 경로는 프로젝트 안으로 제한한다.

| 명령 | 역할 / 부작용 |
|---|---|
| `python main_rmmt.py` / `service` | 지속 운영, 설정에 따라 실주문 가능 |
| `check` | 설정·준비 조건 확인, KIS 접속·주문·원장 생성 없음; 로그는 기록 가능 |
| `signal --snapshot …` | 로컬 자료 종목선정·보고서, 리밸런싱 주문 없음 |
| `plan --snapshot … --quotes … --at …` | 로컬 계획, 브로커 주문 없음 |
| `paper --snapshot … --quotes … --at …` | 자체 모의체결, PAPER 원장 변경 |
| `dashboard --watch` | 로컬 원장/자료 읽기 전용 화면, KIS 접속 없음 |
| `status [--json]` | 원장 상태 조회 |
| `calendar --start … --end …` | KIS 공식 달력 수집·저장 |
| `collect --date …` | 명시한 날짜 수집·저장 |
| `broker-check` | KIS 읽기 연결 점검, 주문 없음 |
| `init-ledger` | 모드별 원장 초기화, 실제 계좌 잔고를 자동 귀속하지 않음 |
| `run` | 단회 실행, LIVE 설정이면 실주문 가능 |
| `reconcile` | 저장 주문의 브로커 결과 대조, 원장 변경 가능 |
| `bind-order --intent … --broker-id … --branch …` | UNKNOWN에 확인한 증권사 주문번호를 명시 귀속 |
| `reconcile-cost --intent … --fee … --tax …` | 확인한 최종 비용 차액 반영 |
| `cash-event --id … --type … --amount …` | DIVIDEND/FEE_ADJUSTMENT/RECOVERY 현금 사건 |
| `corporate-action --id … --symbol … --quantity … --evidence …` | 근거 있는 권리변동 반영 |
| `set-capital --amount …` | 초기자산·현금을 바꾸지 않고 투자상한 변경 사건 기록 (원 단위, 만원 배수) |

운영 중 투자상한을 바꿀 때 .env의 REVERSE_MOMENTUM_CAPITAL_MANWON을 수정하고 재시작한다. 쓰기 가능한 원장을 열 때 투자상한을 자동 동기화하고 CAPITAL_LIMIT_CHANGE 감사 사건(source=ENV)을 기록한다. 현금·초기자산·보유수량·예약금은 변경하지 않는다. 읽기 전용 조회는 원장을 변경하지 않는다. 기존 set-capital 명령은 선택적으로 남지만 다음 시작에서는 .env가 우선한다. UNKNOWN은 증권사 주문 내역을 확인하여 bind-order로 귀속한 후 reconcile한다. 접수 여부가 불명확한 주문을 DB 삭제로 해결하거나 재매수하지 않는다. Ctrl+C는 정상 종료 요청으로 다루지만 증권사에 접수된 주문은 프로세스 종료만으로 없어지지 않는다.

백업은 원장 SQLite와 WAL/SHM 및 설정·스냅샷·로그를 일관된 시점에 보존해야 한다. 원장 재생성은 기존 실제 보유의 귀속을 잃으므로 임의 삭제하지 않는다. 외부 입출금·배당·권리변동의 자동 완전 수집을 가정하지 말고 확인 자료로 사건을 기록한다.

## 10. 검증과 구현 범위

`python3 -m unittest discover -s Etc/tests`로 자동 검증한다. 테스트는 합성 자료·목/자체 모의체결을 중심으로 하며 실제 실계좌 주문을 실행하여 통과시킨 검증이 아니다. 변경 영역에 맞는 테스트를 수행하고 승인 없이 LIVE 서비스를 시작하지 않는다.

현재 제한: RMMT 웹소켓 미구현, DEMO 주문 차단, 실제 공유계좌 현금 배분은 사용자 관리, 모든 주문 직전 필터 분기의 운영 알림 완전성 미보장, 공식 과거 경고 이력 기반 백테스트 완전성 미확보, 단일 KIS 가격 공급원 아님, 넓은 테이블의 자동 화면 분할 없음. 기존 연구용 웹 대시보드와 실전 터미널 화면은 분리되어 있다.

## 11. 문서 유지 관리 계약

프로그램·설정·데이터 계약·상태 저장·CLI·화면·로그를 변경하면 사용자가 별도로 문서 갱신을 요청하지 않아도 이 문서를 같은 작업에서 갱신한다. 관련 .env.example 한글 주석과 docs의 직접 관련 설명도 일치시킨다. 변경 승인 없이 문서를 근거로 런타임 동작을 임의 변경하지 않는다. AGENTS.md의 사용자 승인 원칙을 유지한다.

변경 시: 코드의 실제 동작 확인 → 관련 절·설정 목록 수정 → 현재 제한 및 검증 결과 수정 → 변경 이력 추가. 미구현 제안은 구현 사실과 구분한다. 비밀값·실계좌 인증 정보는 문서에 복사하지 않는다. 자동 파일 감시 데몬을 설치하는 규칙이 아니라, 앞으로 이 프로젝트에서 수행하는 변경 작업의 동시 문서화 규칙이다.

## 12. 전체 환경설정 사전

다음 값은 **.env.example의 양식 값**이다. 실제 현재 운영값이 아니며, 비밀정보 항목은 값을 의도적으로 생략한다. 각 항목의 구체적 용도는 양식의 한글 주석을 기준으로 기록한다. bool은 true/false, 날짜는 ISO YYYY-MM-DD, 시각은 HH:MM이다. 초 단위 간격·최대 페이지 등은 양수이며 loader의 범위 검증을 따른다.

| 설정키 | 양식 값 | 의미 |
|---|---|---|
| `STRATEGY` | `reverse` | RMMT 전용 환경설정: 일정은 한국시간(KST) 기준이며 설정 변경은 재시작 후 적용됩니다. 비밀값을 공개하지 마세요. 전략 유형: reverse는 역모멘텀 전략입니다. |
| `MARKET` | `KOSDAQ` | 투자 대상 시장: KOSDAQ(코스닥), KOSPI(코스피), COMBINED(두 시장 통합). 통합은 합친 전체 보통주의 시총으로 십분위를 계산합니다. |
| `DECILE` | `1` | 시총 십분위: 0=전체, 1=최소 시총 집단, 10=최대 시총 집단. 0~10 정수만 허용합니다. |
| `SKIP` | `0` | 포메이션 끝과 가격 기준일 사이의 건너뛰기 기간: 현재 0만 지원하며 직전 거래일 종가까지 사용합니다. |
| `HOLDINGS` | `20` | 목표 선정 종목 수: 양의 정수로 설정하며 상한은 없습니다. 기본값은 20개이고 적격 종목이 부족하면 실제 선정 수로 균등 배분합니다. |
| `TIMEZONE` | `Asia/Seoul` | 전략 일정의 기준 시간대: Asia/Seoul(한국시간)입니다. |
| `REVERSE_EXECUTION_TIME` | `10:00` | 예정 거래일의 종목선정 시작 시각(KST, HH:MM): 09:00~15:00 설정 가능. 선정 완료 직후 리밸런싱하며 30분 뒤 주문창을 종료합니다. |
| `EXECUTION_WINDOW_MINUTES` | `30` | 주문창 길이(분): 현재 30분만 지원하며 설정한 시작 시각에서 30분 뒤 잔량 취소를 요청합니다. |
| `LATE_START_POLICY` | `WITHIN_WINDOW_ONLY` | 늦은 시작 정책: WITHIN_WINDOW_ONLY는 주문창 안에서만 최초 실행을 허용합니다. |
| `REVERSE_MOMENTUM_CAPITAL_MANWON` | `1000` | 매매 가능 투자상한(만원 단위 양의 정수); 1000=1,000만원=10,000,000원 |
| `CAPITAL_POLICY` | `FIXED_CAP` | 자본 운용 정책: FIXED_CAP은 수익이 나도 설정 투자상한을 유지합니다. |
| `MIN_PRICE` | `1000` | 직전 거래일(신호 가격 기준일) 명목종가의 최소 허용 가격(원)입니다. |
| `NO_TRADE_DAYS` | `5` | 보유종목을 0원 평가하는 연속 무거래 시장거래일 수: 현재 5일입니다. 실제 수량은 유지합니다. |
| `PERSISTENT_JUMP_PCT` | `35` | 급변 관찰을 시작하는 가격 변동률의 절댓값 기준(%): 현재 35를 초과할 때입니다. |
| `BUY_FEE` | `0.00015` | 매수 수수료 추정 비율: 0.00015는 0.015%입니다. 실제 비용 확인 후 차액을 조정합니다. |
| `SELL_FEE` | `0.00015` | 매도 수수료 추정 비율: 0.00015는 0.015%입니다. 실제 비용 확인 후 차액을 조정합니다. |
| `SELL_TAX` | `0.002` | 매도세 추정 비율: 0.002는 0.2%입니다. 실제 비용 확인 후 차액을 조정합니다. |
| `ORDER_POLICY` | `MARKET` | 주문 방식: MARKET은 시장가, LIMIT은 지정가입니다. |
| `LIMIT_POLICY` | `CURRENT_NO_CHASE` | 지정가 정책: CURRENT_NO_CHASE는 최초 현재가로 주문하고 추격 없이 주문창 종료 시 잔량을 취소합니다. |
| `SHOCK_POLICY` | `EXCLUDE_EXIT_REBALANCE` | 지속 급변 처리: EXCLUDE_EXIT_REBALANCE는 신규 매수 제외를 유지하고 기존 보유를 다음 리밸런싱에서 매도합니다. |
| `DIVIDEND_POLICY` | `RETAIN` | 배당 운용 정책: RETAIN은 확인된 RMMT 귀속 배당을 전략 현금에 유지합니다. |
| `DESIGN_APPROVED` | `true` | 세부 전략 설계 승인 확인: true여야 브로커 주문 모드를 사용할 수 있습니다. 실거래 승인과는 별개입니다. |
| `RUN_MODE` | `DRY_RUN` | 실행 모드: DRY_RUN은 주문 없음, PAPER는 자체 모의체결, LIVE는 실거래입니다. 현재 DEMO 주문 실행은 차단됩니다. |
| `RMMT_LIVE_ENABLED` | `false` | 실거래 활성화 여부: 별도 승인 후에만 true로 변경합니다. LIVE 모드와 다른 안전 조건도 함께 충족해야 합니다. |
| `KIS_ENV` | `real` | KIS 접속 환경: real은 실전 서버, demo는 모의 서버입니다. |
| `KIS_REAL_APP_KEY` | 비공개 / 미기입 | KIS 실전 API 앱키(비밀정보): 로그와 보고서에 출력하지 않습니다. |
| `KIS_REAL_APP_SECRET` | 비공개 / 미기입 | KIS 실전 API 앱시크릿(비밀정보): 로그와 보고서에 출력하지 않습니다. |
| `KIS_REAL_ACCOUNT` | 비공개 / 미기입 | KIS 실전 계좌번호(비밀정보): 앞 8자리 계좌번호와 뒤 2자리 상품코드를 설정합니다. |
| `KIS_DEMO_APP_KEY` | 비공개 / 미기입 | KIS 모의 API 앱키(비밀정보): 로그와 보고서에 출력하지 않습니다. |
| `KIS_DEMO_APP_SECRET` | 비공개 / 미기입 | KIS 모의 API 앱시크릿(비밀정보): 로그와 보고서에 출력하지 않습니다. |
| `KIS_DEMO_ACCOUNT` | 비공개 / 미기입 | KIS 모의 계좌번호(비밀정보): 앞 8자리 계좌번호와 뒤 2자리 상품코드를 설정합니다. |
| `KIS_ACCOUNT_PRODUCT_CODE` | 비공개 / 미기입 | KIS 계좌 상품코드: 값이 있으면 계좌번호 뒤 2자리보다 우선하며 일반 위탁계좌는 보통 01입니다. |
| `KIS_REST_MIN_INTERVAL_SECONDS` | `0.2` | RMMT의 KIS REST 요청 사이 최소 간격(초)입니다. |
| `POLL_SECONDS` | `3` | 주문창 안의 체결 확인 및 서비스 점검 간격(초)입니다. |
| `QUOTE_MAX_AGE_SECONDS` | `30` | 주문 판단에 사용할 현재가의 최대 허용 경과시간(초)입니다. |
| `STATUS_MAX_AGE_SECONDS` | `60` | 매수 판단에 사용할 경고 상태 자료의 최대 허용 경과시간(초)입니다. |
| `ACCOUNT_POLICY` | `SHARED_OWNED_ONLY` | 계좌 운용 정책: SHARED_OWNED_ONLY는 양 전략이 자기 귀속수량만 매도하는 동일 계좌 병행 정책입니다. |
| `BCSO_STOP_CONFIRMED` | `false` | BCSO_STOPPED 계좌 정책을 선택했을 때 BCSO의 실제 중지 완료 확인값입니다. |
| `BCSO_REFERENCE_ACCOUNT_SALT` | 비공개 / 미기입 | 기존 BCSO 계좌 지문 생성용 비밀값입니다. 계좌 분리 확인에 사용하며 임의 변경하지 않습니다. |
| `BCSO_REFERENCE_ACCOUNT_HASH` | 비공개 / 미기입 | 기존 BCSO 계좌의 보호된 지문입니다. 계좌 분리 확인에 사용하며 임의 변경하지 않습니다. |
| `BCSO_OWNERSHIP_FIX_CONFIRMED` | `false` | BCSO가 자기 귀속수량만 매도하도록 수정된 사실의 확인값입니다. 사용자가 수정 완료를 확인한 후에만 true로 변경합니다. |
| `SHOCK_OBSERVATIONS` | `5` | 급변 이후 지속 여부를 확인할 유효 실제 거래 관측 수: 현재 5회입니다. |
| `RMMT_DATA_DIR` | `rmmt/data` | 원본 자료·스냅샷·이력 캐시를 저장할 기본 데이터 폴더입니다. RMMT 폴더 안의 경로만 허용합니다. |
| `RMMT_RESULTS_DIR` | `rmmt/results` | 실행별 CSV·JSON 보고서를 저장할 기본 결과 폴더입니다. |
| `CALENDAR_PATH` | `rmmt/data/calendar.json` | 공식 시장거래일 달력 JSON 파일 경로입니다. |
| `SIGNAL_SNAPSHOT_PATH` | `rmmt/data/signal_latest.json` | 예정 거래일 설정 시각 선정 결과의 입력 스냅샷 JSON 경로: 가격·시총 기준일은 직전 거래일입니다. |
| `DAILY_SNAPSHOT_PATH` | `rmmt/data/daily_latest.json` | 최신 완료 거래일의 보유종목 평가 입력 JSON 파일 경로입니다. |
| `QUOTE_SNAPSHOT_PATH` | `rmmt/data/examples/synthetic_quotes.json` | 계획·자체 모의체결에 사용할 현재가 JSON 파일 경로입니다. 기본 예시는 합성 자료입니다. |
| `DRY_RUN_DB_PATH` | `rmmt/data/dry_run.sqlite` | 주문 없는 DRY_RUN 모드의 독립 SQLite 원장 경로입니다. |
| `PAPER_DB_PATH` | `rmmt/data/paper.sqlite` | 자체 모의체결 PAPER 모드의 독립 SQLite 원장 경로입니다. |
| `DEMO_DB_PATH` | `rmmt/data/demo.sqlite` | KIS 모의환경 DEMO 모드의 독립 SQLite 원장 경로입니다. 현재 DEMO 주문 실행은 지원하지 않습니다. |
| `LIVE_DB_PATH` | `rmmt/data/live.sqlite` | 실거래 LIVE 모드의 독립 SQLite 원장 경로입니다. 다른 모드의 원장과 공유하지 않습니다. |
| `KIS_REAL_TOKEN_CACHE_PATH` | `rmmt/data/.kis_token_real.json` | API가 발급한 실전 접속 토큰의 RMMT 전용 캐시 경로입니다. 파일 권한은 0600으로 관리합니다. |
| `KIS_DEMO_TOKEN_CACHE_PATH` | `rmmt/data/.kis_token_demo.json` | API가 발급한 모의 접속 토큰의 RMMT 전용 캐시 경로입니다. 파일 권한은 0600으로 관리합니다. |
| `SERVICE_AUTO_COLLECT` | `true` | 서비스의 마감 후 자동 데이터 수집 여부: true이면 DATA_READY_TIME 이후 KIS·Yahoo 자료를 수집합니다. |
| `SERVICE_IDLE_SECONDS` | `30` | 설정한 주문창 이외의 서비스 반복 대기시간(초)입니다. |
| `SERVICE_ERROR_RETRY_SECONDS` | `15` | 서비스 오류가 발생한 뒤 다음 점검 전에 추가로 기다릴 시간(초)입니다. |
| `HTTP_CONNECT_TIMEOUT_SECONDS` | `5` | HTTP 연결 수립 제한시간(초)입니다. |
| `KIS_REQUEST_TIMEOUT_SECONDS` | `20` | KIS REST 조회·주문 응답 및 토큰 발급 응답의 읽기 제한시간(초)입니다. |
| `PUBLIC_DATA_TIMEOUT_SECONDS` | `30` | 공개 종목 마스터와 Yahoo 자료 응답의 읽기 제한시간(초)입니다. |
| `KIS_HASH_TIMEOUT_SECONDS` | `15` | KIS 주문 본문 해시 발급 응답의 읽기 제한시간(초)입니다. |
| `KIS_TOKEN_EXPIRY_MARGIN_SECONDS` | `60` | 토큰 만료 전에 갱신 필요 상태로 판단할 여유시간(초)입니다. |
| `KIS_TOKEN_ISSUE_COOLDOWN_SECONDS` | `60` | 토큰 발급 재시도 사이 최소 대기시간(초): 60초 이상이어야 합니다. |
| `KIS_MASTER_CACHE_SECONDS` | `45` | KIS 공식 종목 마스터를 재사용할 최대 캐시 시간(초)입니다. |
| `KIS_ACCOUNT_MAX_PAGES` | 비공개 / 미기입 | 계좌·주문 연속조회에서 허용할 최대 페이지 수입니다. 초과 시 불완전 자료 사용을 중단합니다. |
| `KIS_HISTORY_MAX_PAGES` | `500` | 종목별 과거 일봉 이력 조회에서 허용할 최대 페이지 수입니다. |
| `KIS_HISTORY_OVERLAP_DAYS` | `10` | KIS 일봉 캐시 갱신 시 다시 조회할 최근 중첩 구간 길이(달력일)입니다. |
| `KIS_HISTORY_CACHE_MAX_GAP_DAYS` | `100` | 최신 캐시와 수집일 사이에서 증분 갱신을 허용할 최대 간격(달력일)입니다. 초과 시 전체 이력을 다시 수집합니다. |
| `COLLECTION_PROGRESS_INTERVAL` | `100` | 시총·이력 수집의 진행 알림 간격(종목 수) 및 달력 수집의 진행 알림 간격(조회 날짜 수)입니다. |
| `CALENDAR_MAX_RANGE_DAYS` | `2000` | 한 번에 공식 달력을 수집할 수 있는 시작일·종료일 간 최대 간격(달력일)입니다. |
| `DATA_READY_TIME` | `16:00` | 매 거래일 마감 자료 수집을 시작할 수 있는 시각(HH:MM, KST): 다음 예정일 설정 시각 선정에 사용할 자료를 미리 저장합니다. |
| `KIS_REAL_ACCOUNT_PASSWORD` | 비공개 / 미기입 | 선택적 실전 계좌 비밀번호(비밀정보): 현재 KIS REST 구현에서는 사용하거나 전송하지 않습니다. |
| `KIS_DEMO_ACCOUNT_PASSWORD` | 비공개 / 미기입 | 선택적 모의 계좌 비밀번호(비밀정보): 현재 KIS REST 구현에서는 사용하거나 전송하지 않습니다. |
| `KIS_REAL_HTS_ID` | 비공개 / 미기입 | 선택적 KIS 실전 HTS 사용자 ID: 현재 REST 구현에서는 사용하거나 전송하지 않습니다. |
| `KIS_DEMO_HTS_ID` | 비공개 / 미기입 | 선택적 KIS 모의 HTS 사용자 ID: 현재 REST 구현에서는 사용하거나 전송하지 않습니다. |
| `EXCLUDE_REGULATORY_MANAGED` | `true` | 관리종목 제외 여부: true는 제외 적용, false는 이 경고 자체로는 제외하지 않음입니다. |
| `EXCLUDE_REGULATORY_CAUTIONARY` | `true` | 투자주의환기종목 제외 여부: true는 제외 적용, false는 이 경고 자체로는 제외하지 않음입니다. |
| `EXCLUDE_REGULATORY_CAUTION` | `true` | 투자주의종목 제외 여부: true는 제외 적용, false는 이 경고 자체로는 제외하지 않음입니다. |
| `EXCLUDE_REGULATORY_WARNING` | `true` | 투자경고종목 제외 여부: true는 제외 적용, false는 이 경고 자체로는 제외하지 않음입니다. |
| `EXCLUDE_REGULATORY_RISK` | `true` | 투자위험종목 제외 여부: true는 제외 적용, false는 이 경고 자체로는 제외하지 않음입니다. |
| `SYSTEM_LOG_PATH` | `rmmt/logs/system.log` | 운영 상태와 주문 상태 변경 및 오류 기록 경로 |
| `TRADING_LOG_PATH` | `rmmt/logs/trading.log` | 확정 체결과 비용 정정 CSV 기록 경로; 전체 이력 유지 |
| `SCREEN_LOG_PATH` | `rmmt/logs/screen.log` | 화면 출력 및 상세 JSON 진단 기록 경로: 상세 JSON은 대시보드에서 생략해도 파일에는 남습니다. |
| `LOG_MAX_BYTES` | `10485760` | system 및 screen 로그의 파일당 최대 바이트; 초과 시 회전 |
| `LOG_BACKUP_COUNT` | `5` | system 및 screen 로그별 이전 파일 보관 개수 |
| `DASHBOARD_ENABLED` | `true` | 터미널 대시보드 표시 여부: true는 필요한 상태·알림 중심 화면, false는 명령의 JSON 출력을 사용합니다. |
| `DASHBOARD_REFRESH_SECONDS` | `3` | 대시보드의 일반 갱신 간격(초): 상태 변경·수집 진행 알림은 즉시 갱신할 수 있습니다. |
| `DASHBOARD_RECENT_LINES` | `10` | 대시보드에 표시할 최근 중요 알림의 최대 줄 수: 반복 대기는 생략하고 진행률은 최신 값으로 갱신합니다. |
| `BOOTSTRAP_CALENDAR_PAST_DAYS` | `120` | 과거 달력 확보의 기본 일수(달력일, 최소 60): 포메이션 길이와 리밸런싱 기준일에 맞춰 실제 조회 범위를 늘립니다. |
| `BOOTSTRAP_CALENDAR_FUTURE_DAYS` | `45` | 미래 달력 확보의 기본 일수(달력일, 최소 14): 리밸런싱 간격에 맞춰 실제 조회 범위를 늘립니다. |
| `PREPARATION_STATE_PATH` | `rmmt/data/preparation_state.json` | 마감 자료 수집 완료 상태 기록 파일; 재시작 시 중복 수집 방지 |
| `FORMATION_DAYS` | `21` | 포메이션 수익률 계산 간격(거래일, 정수 5~504): 1주=5, 1개월=21, 1년=252, 24개월=504. 종가 자료는 설정값+1개가 필요합니다. |
| `REBALANCE_DAYS` | `5` | 선정·리밸런싱 간격(거래일, 정수 1~252): 매일=1, 1주=5, 1개월=21, 1년=252. 주·월·년 환산은 근사치이며 휴장일은 세지 않습니다. |
| `REBALANCE_ANCHOR_DATE` | `2026-10-12` | 선정·리밸런싱 시작 기준일(YYYY-MM-DD): 휴장이면 다음 첫 거래일을 기준으로 N거래일마다 설정 시각 선정 후 즉시 매매합니다. 재시작해도 기준은 유지됩니다. |
| `PREVIEW_HOLDING_ENABLED` | `false` | 대시보드 표시용 가상 보유: false로 바꾸면 제거합니다. 실제 원장·매매·전략손익에는 반영하지 않습니다. |
| `PREVIEW_HOLDING_SYMBOL` | `005930` | 표시용 가상 보유 종목코드 |
| `PREVIEW_HOLDING_NAME` | `삼성전자` | 표시용 가상 보유 종목명 |
| `PREVIEW_HOLDING_QUANTITY` | `10` | 표시용 가상 보유 수량(주) |
| `PREVIEW_HOLDING_PRICE` | `271500` | 표시용 가상 보유 평단 및 평가가격(원) |
| `DASHBOARD_QUOTE_REFRESH_SECONDS` | `5` | 보유·선정·가상 종목의 대시보드 현재가 REST 조회 최소 간격(초): 웹소켓이 아닌 주기 조회이며 조회량에 따라 지연될 수 있습니다. |
| `PREVIEW_EXTRA_HOLDINGS` | `9종목 예시 목록 (형식은 아래 설명)` | 추가 화면 표시용 가상 보유: 코드,이름,수량,가상평단(원); 종목 간 세미콜론 구분. 실제 매매·원장·손익에 반영하지 않으며 비우면 제거됩니다. 평단은 시세가 아닌 예시값입니다. |

추가 가상 보유 형식: `PREVIEW_EXTRA_HOLDINGS=000660,SK하이닉스,10,200000;035420,NAVER,10,200000`처럼 코드·이름·수량·평단을 쉼표, 종목을 세미콜론으로 구분한다. 기본 가상 종목을 포함해 종목코드는 중복되지 않는 6자리 숫자이고 수량·평단은 양수여야 한다.

설정 중 일부는 현재 고정 계약이다: STRATEGY=reverse, SKIP=0, TIMEZONE=Asia/Seoul, NO_TRADE_DAYS=5, PERSISTENT_JUMP_PCT=35, SHOCK_OBSERVATIONS=5, LATE_START_POLICY=WITHIN_WINDOW_ONLY, EXECUTION_WINDOW_MINUTES=30, CAPITAL_POLICY=FIXED_CAP. 설정키가 존재해도 임의 값을 모두 지원한다는 의미는 아니다. 토큰은 사용자 비밀 설정과 별도로 API가 발급한 캐시 파일에 저장한다.

## 13. 변경 이력

- 2026-10-07: 프로젝트 전체 통합 명세 신규 작성. 실전/연구 영역, 거래일 기반 선정·리밸런싱, 공급원, 주문 귀속, 상태 저장·복구, 전체 설정 사전을 기록.
- 2026-10-07: 최근 화면 변경(가상 10종목, 20칸 종목명, 손익 소수점 1자리 및 +/% 생략, 상태 왼쪽 정렬, 빈 테이블 헤더, 합계 행, 중요 차단 로그, 로그 제목 KST 시간)을 현행 동작으로 문서화.
- 2026-10-07: 이후 프로그램·설정 변경 시 본 문서 동시 갱신 지침을 AGENTS.md에 등록.

- 2026-10-07: 현재가 조회 및 KIS 요청/응답 실패의 상세 진단을 system 로그에 추가. 최근 화면 요약 및 주문 정책은 유지.


## 14. 웹 대시보드 독립 패키지 (2026-10-07)

`dashboard/`는 폴더 전체를 다른 위치로 옮겨 실행하는 독립 조회 패키지다. 서버·수익률 매핑 작성기의 ROOT는 해당 파일이 있는 dashboard 폴더이며 Dashboard 대문자 하드코딩을 제거했다. 전용 src에는 filtered_backtest.py와 hybrid_v2_backtest.py 사본을 포함하고 조회에 불필요한 외부 collector import를 제거했다. 전용 prepare는 원본 자료 재생성용이 아님을 명시하여 차단한다.

기존 results/hybrid_v3를 dashboard/results/hybrid_v3로, data/hybrid를 dashboard/data/hybrid로 이동했다. 원본 src의 해당 경로도 새 위치로 수정했다. 나머지 연구 결과와 RMMT rmmt/data·rmmt/results는 이동하지 않았다. 전용 서버는 V3 FORCE_ZERO_HALT=True로 상세를 재생한다. requirements.txt는 numpy/pandas를 지정한다. macOS 실행 스크립트는 macOS 전용이며 Linux는 python3 serve_dashboard.py로 실행한다. RMMT 운영 모듈은 이 패키지를 참조하지 않는다.

- 2026-10-07: 웹 대시보드 관련 소스·결과·자료 내부화 및 상대경로 수정, 독립 실행 안내 추가.

독립 패키지 검증: 프로젝트 외부 cwd에서 두 방향 상세 재생(각 2,852 NAV 시점), HTML의 로컬 CSS/JS 참조와 기본 데이터 파일 존재 확인, RMMT 자동 테스트 129개 통과. 샌드박스의 로컬 포트 바인딩 제한으로 HTTP 서버 통합 검증은 수행하지 못했다. 실제 KIS 접속·주문은 실행하지 않았다.


## 15. 대시보드 외부 이동 (2026-10-07)

독립 dashboard 패키지는 사용자 요청으로 `/Users/mikepark/googleDrive/Codes/Stocks/StockAnalysis/dashboard`로 이동했다. 현재 RMMT 프로젝트 루트에는 dashboard 폴더가 없다. 해당 폴더에서 requirements 설치 및 serve_dashboard.py 실행을 수행한다. 폴더 내부 서버 경로는 자체 위치 기준이므로 추가 수정 없이 이동할 수 있다. RMMT main_rmmt.py는 영향을 받지 않는다. 원본 연구 src에서 이동한 hybrid 자료·V3 결과를 참조하는 경로는 새 외부 위치로 갱신했다. 이 외부 경로는 연구 스크립트에만 해당하며 AWS 실전 운영 의존성이 아니다.

- 2026-10-07: dashboard 전체를 StockAnalysis/dashboard로 이동. 원본 연구 참조 및 프로젝트 명세 갱신.


## 16. 실전 운영 폴더 통합 (2026-10-07)

실행 중인 main_rmmt.py가 없음을 확인하고 원장 잠금을 확보한 뒤 기존 data/rmmt → rmmt/data, logs → rmmt/logs, results/rmmt → rmmt/results로 이동했다. 원장 내용은 이동 전후 해시가 동일하고 SQLite quick_check가 통과했다. 운영 소스와 운영 저장소가 rmmt 아래에 함께 존재한다. data.py 모듈과 data 저장 폴더는 서로 다른 이름의 파일·디렉터리로 공존하며 저장 폴더는 Python 패키지로 만들지 않는다.

현재 구조:

```text
Reverse_Momentum/
├── main_rmmt.py
├── .env
├── .env.example
├── requirements-rmmt.txt
├── specification.md
├── AGENTS.md
├── rmmt/
│   ├── *.py
│   ├── data/       # 원장·달력·토큰·스냅샷·캐시
│   ├── logs/       # system.log / screen.log / trading.log 및 회전 파일
│   └── results/    # 전략명/실행별 보고서
├── Etc/src/            # 연구 전용
├── data/           # 남은 연구 자료
└── results/        # 남은 연구 결과
```

.env 및 .env.example, Config 기본값은 프로젝트 루트 기준 짧은 상대경로를 사용한다. 로그 파일명은 system.log, screen.log, trading.log로 줄였다. 기존 회전 파일도 대응 이름으로 옮겼다. 보고서의 과거 config.json 등은 실행 당시 증거이므로 이전 경로를 소급 수정하지 않는다. 준비 상태의 과거 설정 해시는 변경될 수 있으므로 현재 설정 검증에 따라 재준비하며 기존 주문 사건을 삭제하지 않는다.

AWS 최소 복사: main_rmmt.py, .env, requirements-rmmt.txt, rmmt/ 전체. 유지보수 문서 및 .env.example/AGENTS.md도 권장한다. __pycache__는 제외하고 AWS에서 의존성을 설치한다. 원장 싱크 전에 서비스 종료, SQLite WAL/SHM 보존, 로컬과 AWS 중 한 곳에서만 LIVE 실행한다. 원장 경로를 옮긴 뒤에는 옛 경로로 원장을 새로 만들지 않는다. 실행 명령은 프로젝트 루트에서 python main_rmmt.py 그대로다.

검증: 실제 운영 설정의 새 경로 및 원장·로그 존재 확인, SQLite 이동 전후 해시·quick_check, 자동 테스트 129개 통과. KIS 접속 및 실주문은 실행하지 않았다.

- 2026-10-07: 실전 운영 저장소를 rmmt/data, rmmt/logs, rmmt/results로 통합하고 로그 이름·설정·테스트·운영 문서를 갱신.


## 17. 비운영 폴더 Etc 통합 (2026-10-07)

루트의 rmmt를 제외한 일반 폴더를 Etc로 이동했다. 숨김 관리 폴더가 존재하면 .git 등은 이동 대상에서 제외한다. Etc/src·Etc/data·Etc/results·Etc/Reference·Etc/output은 연구 영역, Etc/docs는 개발/운영 참고문서, Etc/tests는 검증 코드, Etc/tmp는 임시 자료다. 기존 루트 __pycache__도 Etc/__pycache__로 이동했지만 Python 실행 시 루트 캐시는 다시 생성될 수 있다.

연구 코드의 Path(__file__).resolve().parents[1]은 이동 뒤 Etc를 가리키므로 같이 이동한 연구 자료 상대경로가 유지된다. 이미 외부 StockAnalysis/dashboard를 참조하는 경로는 그대로 유지한다. 테스트의 임시 경로는 Etc/tmp, fixture import는 Etc.tests.fixtures로 변경했다. 루트 .env/양식과 main_rmmt.py, requirements-rmmt.txt, AGENTS.md, specification.md는 유지한다. 실전 rmmt/data·logs·results에는 변화가 없다.

```text
Reverse_Momentum/
├── main_rmmt.py / .env / requirements-rmmt.txt
├── specification.md / AGENTS.md / .env.example
├── rmmt/          # 실전 소스·data·logs·results
└── Etc/
    ├── src/
    ├── data/
    ├── results/
    ├── Reference/
    ├── output/
    ├── docs/
    ├── tests/
    ├── tmp/
    └── __pycache__/
```

검증 명령: `python3 -m unittest discover -s Etc/tests` (프로젝트 루트에서). 개별 검증은 `python3 -m unittest Etc.tests.test_dashboard`처럼 수행한다. AWS 실전 복사에는 Etc가 필요하지 않다.

- 2026-10-07: 비운영 폴더를 Etc로 통합하고 테스트 import·임시 경로·루트 문서 참조를 수정.

Etc 통합 검증: 변경 후 자동 테스트 129개 통과, 연구 Python 소스 구문 확인, 실제 운영 원장 경로 유지 확인. .gitignore의 로그 제외 경로는 rmmt/logs/로 수정했다.

- 2026-10-07: .env 투자상한을 시작 시 원장에 자동 반영하도록 변경. 미완료 주문·예약금·초기자산·현금은 보존, 감사 기록 및 운영 알림 추가. 기존 상한보다 현재 보유 평가액이 커도 즉시 강제 청산하지 않으며 신규 매수 제한과 다음 리밸런싱 목표에 적용한다.
