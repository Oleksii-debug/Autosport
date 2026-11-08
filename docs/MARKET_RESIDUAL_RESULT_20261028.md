# Team outcome residual: exploratory result

Following the frozen protocol, the recorded match comparison produced:

| Metric | Market baseline | Team-residual challenger |
|---|---:|---:|
| Validated matches for Brier | 2357 | 2357 |
| Brier (lower is better) | 0.197598 | 0.197692 |
| Settled hypothetical selections | 88 | 96 |
| Net units after 1% cost | -8.170 | -2.890 |
| ROI after 1% cost | -9.28% | -3.01% |
| Max drawdown after 1% cost | 11.850 | 11.470 |

The challenger is still negative and slightly less accurate overall by Brier.
It produced 62 same-selection events, 34 challenger-only and 27 baseline-only;
these are different hypothetical portfolios. Of 2357 scored matches, 1997 had
at least five previously assumed-revealed results for each named team.
The daily paired bootstrap of total net difference was +5.28 units, with a 95%
interval from -8.45 to +18.93 units across 37 day blocks (10,000 resamples,
seed 28102026). This descriptive interval spans harm and benefit; it does not
establish profit, causality, or statistical significance. Data were inspected
before this comparison and two candidate models were tried; this is not an
untouched holdout and no multiplicity-adjusted significance is asserted.

Post-hoc descriptive split: September -3.680 / -0.560 units (8 / 5 bets),
October -4.490 / -2.330 (80 / 91 bets), baseline / challenger. These are not
independent tests and were not used for tuning. The 48-hour outcome timing,
team name grouping, price timing and execution remain assumptions, as described
in the protocol. The result cannot justify promotion or real-money execution.

12 focused tests passed, including support gating, delayed feedback, future
outcome independence, normalized probability bounds and duplicate rejection.
All decisions, including losses and unplaced results, remain in the full journal;
the compact report records its SHA-256. Source: TipsAudit (2026),
https://tipsaudit.com/data, CC BY 4.0.
