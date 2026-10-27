# Outcome-based agent improvement: first development result

This follows the owner's 2026-10-27 priority: economic usefulness and learning
quality before installation work. It does not change the frozen running sandbox
candidate a1869a94b7cdd2c1e7196adb78d39b557f82afbc.

## Reproducible result

Source attribution: lathise2025, Table Tennis Pre-Match Elo Ratings, CC BY 4.0
sample. Source repository https://github.com/lathise2025/table-tennis-elo-dataset,
exact revision `9eba2ade435ce27b4f3dcbd5aafc6bfbcd52d534`.
The local source revision and LICENSE were read before the experiment.
Input SHA-256:
`bbb2ab531f269a1eafff89e240086a4d17999114b582825ba4338986c6770fde`.
No full commercial dataset was acquired or substituted.

All 2,000 rows were retained. First 28 calendar days: 497 warmup rows.
Evaluation: 1,503 rows, 2026-07-23 through 2026-10-20. The local protocol was
written before inspecting sample outcomes; publisher aggregate results were
already known. This is retrospective development evidence, not a clean holdout.

| Predictor | Brier error | Log loss |
|---|---:|---:|
| Constant 0.5 | 0.250000000 | 0.693147181 |
| Publisher probability | 0.245558235 | 0.684140073 |
| Fixed half-confidence | 0.244779878 | 0.682563987 |
| Learned earlier-date shrinkage | 0.244566524 | 0.682079698 |

Lower is better. The adaptive reduction is about 0.000992 Brier points, or 0.404%
relative error reduction. It loses to the publisher in 7 of 14 calendar-week
groups. The first and last groups are partial weeks. No independent statistical
significance, stable superiority or economic edge is established.

| Competition | Evaluation rows | Publisher Brier | Adaptive Brier |
|---|---:|---:|---:|
| TT Elite Series | 478 | 0.237793 | 0.237698 |
| Setka Cup | 471 | 0.245851 | 0.245092 |
| Czech Liga Pro | 261 | 0.260013 | 0.255817 |
| TT Cup | 231 | 0.247447 | 0.246676 |

The adaptive predictor remains worse than constant 0.5 for Czech Liga Pro.
Small cohorts also regress; every league and week is retained in the JSON.
This motivates a separately labeled competition-conditioned experiment, not
silently editing the first protocol or deploying the aggregate winner.

## What changes for development

1. The sandbox selection fix stops indefinitely forcing empirically losing arms
   after a bounded exploratory warmup. It is already in PR #2583 at a1869a94.
2. The new read-only diagnostic tests learning against published match results.
   It reveals instability hidden by a single aggregate score and checks that
   same-day/future outcomes cannot change earlier predictions.
3. Confidence reduction is a candidate research direction. No runtime model,
   production learning population, risk policy or historical reward is rewritten
   from this external retrospective sample.

To establish profit, the next admissible dataset must include matching observed
pre-decision odds, results, source availability times and cost/fill assumptions.
Neither this sample nor the price-movement sandbox supplies that combination.
Net profit, ROI and drawdown are explicitly null/unavailable in this report.
No pseudo bookmaker odds or synthetic winning payouts fill that gap.

## Validation

13 diagnostic tests passed: date-boundary causality, future-prefix invariance,
input-order invariance, shared denominator, negative-result learning, invalid
probabilities, duplicate event rejection, inconsistent results and insufficient
time coverage. Reproduce from the pinned CC BY sample:

```sh
python scripts/audit_account_c_outcome_calibration.py \
  --csv /path/to/tt-ratings-reference/data/tt_ratings_sample_2000.csv \
  --output /tmp/account-c-calibration.json
```

The JSON binds the input and script hashes. The script does not authenticate the
caller's Git checkout and reports that limitation explicitly; the source revision
was independently checked for this run. Upstream point-in-time lineage, result
publication timestamps and model-training isolation remain unverified.
