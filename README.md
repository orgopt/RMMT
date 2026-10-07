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

```bash
python3 main_rmmt.py check
python3 -m unittest discover -s Etc/tests -v
```

[운영 안내](Etc/docs/RMMT_OPERATIONS.md), [개발 스펙](Etc/docs/RMMT_DEVELOPMENT_SPEC.md), [KIS 계약/검증 범위](Etc/docs/KIS_API_CONTRACT.md)를 확인한다. KRX 로그인 의존성 없이 KIS 시총·원주가·거래량·경고를 사용하고 Yahoo 신호가격을 별도 검증한다. 기존 연구 자료와 BCSO 폴더는 보존한다. 참고 BCSO가 계좌 전체 보유를 매도하므로 같은 계좌 병행은 시간 분리만으로 해결되지 않으며, 사용자가 BCSO 자기 귀속수량 매도 수정을 완료하고 확인하기 전 LIVE를 차단한다.
