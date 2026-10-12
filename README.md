# Reverse_Momentum exploratory backtest

## Status

The new grid is **exploratory, not a fully validated investable backtest**.
Historical regulatory flags, official corporate-action factors and recovery
values for delisted securities are not available in the supplied database.
Do not treat the top-ranked returns as verified investment performance.

The comparison runs from 2015-01-02 through 2026-08-14, the last date with
sufficient market-cap coverage. Price data extend to 2026-10-01, but empty
market-cap snapshots are not used to infer a sell-all signal.

## Agreed grid

- Formation: 1, 2, 3, 4, 6, 9, 12 months (21 trading days per month).
- Skip: none for all; additionally one month for 6, 9, 12 months only.
- Rebalance: 1, 2 weeks; 1, 3, 6, 9, 12 calendar months.
- Markets: KOSPI, KOSDAQ, combined corresponding market-specific deciles.
- Market-cap buckets: each D1-D10, plus the full universe (D0).
- Target slots: 1, 3, 5, 10, 20, 30, 40, 50. Equal allocation, absent slots cash.
- Initial NAV: 1, fractional synthetic units, no currency-specific sizing.
- Buy/sell commission: 0.015% each; sell tax: 0.2%; slippage: 0.
- Grid size: 18,480.

## Applied proxies and missing filters

Current names and share-code conventions approximate common-stock type.
Daily market ranges approximate point-in-time market membership. Listing age
is first-observation age, not official listing age. Trading value is estimated
as raw close times volume. Minimum price is 1,000 KRW, observation age 126
trading days, 60-day mean value 500 million KRW, median 200 million KRW,
positive volume at least 57 of 60 days and all of the last 20 days.

Historical designation/removal of management, caution, warning, and risk status
is **not applied**. Announcement-based corporate-action entry restrictions are
also **not applied**. Discontinuity factors are inferred from large overnight
gaps and common ratios, additionally requiring corroborating market-cap/close
implied share-count ratios within 5%; they are not official event records. Unresolved
gaps quarantine subsequent entries for 273 trading days. These assumptions can
remove real returns or miss corporate actions and must be replaced before
making investment decisions.

Sales on zero-volume days remain frozen. Fees are included in the cash limit.
No future-based delisting heuristic is used; stale last-observed marks can still
overstate recoverable assets. Terminal NAV is marked value, not cash liquidation.
Dividends, participation limits and order-book constraints are not included.

## Run

```sh
OPENBLAS_NUM_THREADS=1 /Users/mikepark/myEnv/bin/python Etc/src/filtered_backtest.py --test
OPENBLAS_NUM_THREADS=1 /Users/mikepark/myEnv/bin/python Etc/src/filtered_backtest.py --cached
/Users/mikepark/myEnv/bin/python Etc/src/build_report.py
```

`--cached` uses the prepared diagnostic arrays and cached metadata; no database
write is performed. `Etc/Reference/` is preserved. Detailed settings are recorded
in `results/manifest.json`. The full CSV and NAV arrays are under `results/`;
the PDF is under `Etc/output/pdf/`.

## Momentum comparison

The high-return-first variant uses the same prepared cache and execution engine.
It writes to `results/momentum/` and leaves the reverse-momentum outputs intact.
Only the ranking direction is changed; eligible stock sets, fees and dates stay
the same. The PDF includes 18,480 matched parameter pairs against the saved
reverse-momentum results.

```sh
OPENBLAS_NUM_THREADS=1 /Users/mikepark/myEnv/bin/python Etc/src/momentum_backtest.py
OPENBLAS_NUM_THREADS=1 /Users/mikepark/myEnv/bin/python Etc/src/momentum_report.py
```

Use `Etc/src/momentum_backtest.py --audit-only` to validate existing momentum outputs
and rebuild the paired comparison without rerunning the grid. All the same
data-quality qualifications apply to this variant.


## RMMT 독립 실행기

메인: `main_rmmt.py`. 기본 설정은 `.env`의 DRY_RUN이며 실거래는 활성화하지 않았다.

`.env`의 `STRATEGY=reverse`는 과거 수익률이 낮은 순으로, `STRATEGY=momentum`은 높은 순으로 종목을 선정한다. 두 전략은 시장·시총 집단·필터·주기·배분·주문 절차를 공유하며 동률은 종목코드 오름차순이다. 모멘텀 전략 ID에는 `_MOMENTUM`이 붙는다. `REVERSE_EXECUTION_TIME`과 `REVERSE_MOMENTUM_CAPITAL_MANWON`은 기존 키 이름을 유지하며 두 전략에 공통 적용한다.

`STRATEGY=momentum`으로 바꾸면 선택 모드의 DB 파일명 앞에 `momentum_`을 자동 적용한다. 기존 `LIVE_DB_PATH=rmmt/data/live.sqlite`는 그대로 두면 모멘텀은 `rmmt/data/momentum_live.sqlite`, 역모멘텀은 `rmmt/data/live.sqlite`를 사용한다. PAPER/DRY_RUN/DEMO도 같은 규칙이다. 사용자 지정 디렉터리와 원래 파일명은 유지하며 파일명에 접두사만 붙인다. 이미 `momentum_`으로 시작하는 파일명은 유지하고, 명령의 `--db`를 지정하면 그 경로가 우선한다. `check` 출력의 `database_path`에서 자동 선택 경로를 확인한다.

기존 역모멘텀 DB를 모멘텀 설정으로 직접 지정하면 원장 유형 검사로 차단한다. 새 원장은 기존 계좌 보유를 자동으로 가져오지 않으며, 기존 역모멘텀 보유·미완료 주문은 원래 설정과 원장에서 관리한다. 두 전략을 병행한다면 선정·평가 자료, 준비 상태, 로그 경로도 분리해야 하며 같은 계좌의 현금·미체결 주문 충돌 검사는 계속 적용된다. 실제 운영 설정 전환·AWS 배포는 자동으로 수행하지 않는다.

```bash
python3 main_rmmt.py check
python3 -m unittest discover -s Etc/tests -v
```

[운영 안내](Etc/docs/RMMT_OPERATIONS.md), [개발 스펙](Etc/docs/RMMT_DEVELOPMENT_SPEC.md), [KIS 계약/검증 범위](Etc/docs/KIS_API_CONTRACT.md)를 확인한다. KRX 로그인 의존성 없이 KIS 시총·수정주가·원주가·거래량·경고를 사용한다. 신호 수익률·급변 판단은 KIS 수정주가(0), 최소가격·평가는 원주가(1)이며 Yahoo 실전 수집·교차검증은 제거했다. 전체 시총으로 MARKET/DECILE을 먼저 정하고 해당 집단과 보유종목의 F+1 가격만 조회한다. 수정주가는 새 기준일에 필요한 구간을 재조회하며 과거 Yahoo 캐시를 섞지 않는다. 매수 중 시간 종료는 WINDOW_CLOSED로 기록한다. 새 계약과 캐시 전환은 루트 [specification.md](specification.md)의 마지막 절을 따른다. 기존 연구 자료와 BCSO 폴더는 보존한다. 참고 BCSO가 계좌 전체 보유를 매도하므로 같은 계좌 병행은 시간 분리만으로 해결되지 않으며, 사용자가 BCSO 자기 귀속수량 매도 수정을 완료하고 확인하기 전 LIVE를 차단한다.


현재 운용 준비는 `python -B collect_kis_history.py --output rmmt/data/kis_history.sqlite`로 전체 보통주의 1년 KIS 수정 일봉과 검증·스냅샷을 단일 파일에 수집한다. 기준일은 장중 당일을 제외한 직전 완료 거래일이다. 완료 파일과 소스를 AWS에 싱크한 뒤 `.env`의 `KIS_HISTORY_BUNDLE_PATH=rmmt/data/kis_history.sqlite`, `DAILY_COLLECTION_TIME=08:00`, `REVERSE_EXECUTION_TIME=13:30`을 확인하고 수집 시각 이전부터 서비스를 실행한다. `DAILY_COLLECTION_TIME`은 한국시간 HH:MM이며 선정 시각보다 빨라야 한다. 예를 들어 `07:30`으로 변경한 뒤 프로그램을 재시작하면 07:30부터 전체 증분 갱신을 시작한다. 서비스는 전 종목의 수정주가 보관 구간과 필요한 원주가·거래량이 모두 완성된 뒤 스냅샷을 발행하고, 선정 시각에 종목선정·매도·매수를 수행한다. 선정 대상 밖의 종목이나 과거 날짜도 누락이 남으면 INCOMPLETE로 기록하고 재조회한다. 긴 수집 공백은 추가 페이지 조회로 채우며 미완료 파일은 매매에 사용하지 않는다. 기존 READY 파일도 전체 수집 완료 계약을 다시 검증한다.
