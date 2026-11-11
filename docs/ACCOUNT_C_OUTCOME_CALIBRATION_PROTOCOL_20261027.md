# Account C: outcome-based development diagnostic

Owner direction, 2026-10-27: prioritize agents' economic usefulness over packaging.
This is additional research scope, not reopening terminal Plan-3 components.
Canonical integration lineage remains PR #2583. Its running sandbox candidate
`a1869a94b7cdd2c1e7196adb78d39b557f82afbc` is left unchanged.

## Fixed experiment before inspecting sample outcomes

Source: lathise2025, Table Tennis Pre-Match Elo Ratings, GitHub commit
`9eba2ade435ce27b4f3dcbd5aafc6bfbcd52d534`, file
`data/tt_ratings_sample_2000.csv`. The repository LICENSE covers this sample under
CC BY 4.0; attribution and source revision must accompany every report.
Source URL: https://github.com/lathise2025/table-tennis-elo-dataset
License URL: https://creativecommons.org/licenses/by/4.0/

Question: does reducing confidence improve prediction error against reported
match winners, rather than against simulated quote movement?

- All valid rows retained, including cold starts and losing predictions. Invalid
  or repeated match identities fail the diagnostic rather than disappearing.
- First 28 calendar days are development warmup; remaining dates are evaluated.
  No re-selection of dates, leagues or parameters after seeing the result.
- Controls: constant 0.5 and the supplied `p_model`.
- Fixed challenger: `0.5 + 0.5 * (p_model - 0.5)`.
- Adaptive challenger: `0.5 + slope * (p_model - 0.5)`, where
  `slope = clip(sum((p_model-.5)*(y-.5))/(1 + sum((p_model-.5)^2)), 0, 1)`.
  Both sums use only strictly earlier dates. Ridge constant 1 is fixed.
  Every row on a date is predicted before any outcome on that date is learned.
- Primary development metric: mean Brier score (lower is better); secondary:
  log loss, daily/weekly paired deltas, and league-level errors. All four arms
  use the exact same evaluation denominator. No selection of a winning arm
  authorizes a deployment or changes product memory.
- Exact output records input hash, script hash, parameters, date range, all arm
  results and day-by-day slope/history counts. Reordering rows cannot affect
  predictions. Altering today's/future outcomes cannot affect today's prediction.

## Evidence limitations

This public retrospective sample has already been analyzed by its publisher.
It is not an untouched product holdout. Pre-match features and results are
publisher assertions, not independently verified provider/point-in-time receipts.
Day-level order is a conservative local training assumption, not proof of actual
result publication time. Upstream model training could overlap these dates.
The sample selection and recurring-player dependence also limit generalization.
No independent-observation confidence interval or promotion claim is issued.

The sample contains no bookmaker odds, accepted prices, fees or fill evidence.
Profit, ROI, executable edge and bankroll change are therefore UNAVAILABLE, never
zero or inferred from prediction accuracy. This audit cannot enter the production
forward-learning population or PaperBook. No source-rights, scientific or
settlement authority is fabricated. Real-money execution and promotion stay false.
