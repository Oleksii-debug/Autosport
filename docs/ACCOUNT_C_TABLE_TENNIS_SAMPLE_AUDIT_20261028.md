# Exploratory probability audit on a public table-tennis sample

## Attribution and scope

The 2,000-row sample `research/data/tt_ratings_sample_2000.csv` is by
[lathise2025](https://github.com/lathise2025/table-tennis-elo-dataset),
retrieved from repository revision `9eba2ade435ce27b4f3dcbd5aafc6bfbcd52d534`.
The repository states that **only the sample and documentation** are licensed
under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the larger
dataset has separate terms. The sample is reproduced unmodified. Its SHA-256 is
`bbb2ab531f269a1eafff89e240086a4d17999114b582825ba4338986c6770fde`.
This report adds analysis; it does not alter the sample. The publisher describes
its probabilities and ratings as pre-match, but the data's capture timing,
source identity, training history and match outcomes are not independently verified.

There are 2,000 distinct match IDs, dated 2026-06-25 through 2026-10-20. There
are no bookmaker odds, executable quotes, fees or settlement records, so this
cannot establish wagering profit. The sample is not an untouched holdout:
publisher model fitting and sample selection are not independently known, and
the sample and its results are now inspected. Date fields have day resolution.

## Chronological descriptive comparison

We fit a bounded shrinkage coefficient toward 50% on the 1,181 records before
2026-09-01 and apply it to the 819 later records. The coefficient is 0.726814;
no later labels enter its calculation. Brier score measures probability error
(lower is better); accuracy chooses A when the forecast is exactly 50%.

| Later-sample forecast | Brier score | Accuracy |
|---|---:|---:|
| Constant 50% | 0.250000 | 48.47% |
| Publisher's probability | 0.244679 | 54.82% |
| Earlier-date fitted shrink | 0.243634 | 54.82% |

The differences are descriptive and small. The same shrink factor preserves
the predicted side, hence identical accuracy to the supplied forecast. The
four larger league groups vary: publisher Brier is 0.2364 for TT Elite Series,
0.2468 for Setka Cup, 0.2510 for Czech Liga Pro and 0.2516 for TT Cup. Small
groups and selection effects limit inference; these are not validated league
edges. No statistical significance, out-of-sample improvement, agent learning,
stake execution, or profit is asserted.

Reproduce with `PYTHONPATH=src python scripts/audit_table_tennis_probability_sample.py`.
The companion JSON binds source and analysis hashes; focused tests check that
later outcomes do not affect the fitted coefficient and invalid rows are
rejected. The candidate is research only and must not be promoted to runtime
without separately sourced, point-in-time prices and results, independent
chronological evaluation, costs and executable-price evidence.
