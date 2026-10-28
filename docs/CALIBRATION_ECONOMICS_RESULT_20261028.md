# Calibration economics: retained negative result

The predeclared comparison uses the recorded TipsAudit dataset and code in
`CALIBRATION_ECONOMICS_PROTOCOL_20261028.md`. The decision journal and exact
report are retained outside Git with SHA-256 in the compact JSON evidence.

| Metric | Unchanged market reference | Calibrated challenger |
|---|---:|---:|
| Scored matches | 2357 | 2357 |
| Brier (lower is better) | 0.197598 | 0.198165 |
| Settled hypothetical selections | 88 | 131 |
| Net units after 1% cost | -8.170 | -14.770 |
| ROI after 1% cost | -9.28% | -11.27% |
| Max drawdown after 1% cost | 11.850 | 17.170 |

There were 30 same-selection events, one differing selection, 58 baseline-only
and 102 challenger-only candidate events; daily limits can make either variant
use a different cohort. Day-block uncertainty intervals cover zero. This
comparison did not show an improvement and does not justify using this
calibration for product betting decisions. It also does not prove the true edge
of either model: quote/result timing is not independently verified, the 48-hour
label delay is assumed, executable prices and overlapping bankroll are unknown,
and this dataset was previously inspected. No thresholds were tuned after the
result. The separate prospective forecast-scoring campaign remains useful to
observe future accuracy; it has no financial authority.

13 focused tests passed on Linux/Python 3.12 including causal label delay,
unchanged choice when the same match outcome changes, the five-candidate cap on
void/pending rows, duplicate identities, and the existing observer's chronology
and correction tests. Exact reporting source SHA-256:
`c7964f06b9aa02800381c50aca4637ef8c1ad7e940855d3f9c3b5f1bd40e53c5`.
