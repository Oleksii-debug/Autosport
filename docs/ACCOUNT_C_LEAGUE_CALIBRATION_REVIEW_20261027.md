# Competition-conditioned calibration: second development experiment

This experiment follows the first result after inspecting its league breakdown.
The input and first evaluation are already consumed development data, not a new
holdout. Source: lathise2025, Table Tennis Pre-Match Elo Ratings, CC BY 4.0,
https://github.com/lathise2025/table-tennis-elo-dataset, revision
`9eba2ade435ce27b4f3dcbd5aafc6bfbcd52d534`, CSV SHA-256
`bbb2ab531f269a1eafff89e240086a4d17999114b582825ba4338986c6770fde`.
The reported point-in-time forecasts and match outcomes have not been independently
authenticated. No executable prices, costs or fills are included.

Following the separately written protocol, 497 earlier rows warm up the model and
all 1,503 later rows are evaluated. A league-specific slope learns only from prior
dates and is shrunk toward the global slope. Same-day outcomes are not used.

| Predictor | Brier error | Log loss |
|---|---:|---:|
| Publisher | 0.245558235 | 0.684140073 |
| Global past-date shrinkage | 0.244566524 | 0.682079698 |
| League-conditioned shrinkage | 0.244570952 | 0.682133442 |

Lower is better. The league-conditioned candidate is slightly worse overall than
the global candidate, although it improves Czech Liga Pro from 0.255817 to 0.253305
on 261 rows. It worsens Setka Cup from 0.245092 to 0.246177 on 471 rows. Its weekly
results are better in 7 and worse in 7 of 14 weeks. No independence or significance
claim is made. Every week and league, including small groups, remains in the JSON.

Decision: retain this as a negative/mixed research finding. Do not change the
production strategy or memory on its basis; test a separately frozen candidate on
new, lawfully available outcome-and-price data. No parameter optimization was
performed for this experiment. No net profit, ROI or drawdown can be inferred.

Seventeen tests passed across both diagnostics, including league-specific learning,
unknown-league fallback, day-boundary and future causality, common evaluation
population, order invariance, negative cases and prevention of promotion claims.
The original experiment's result and original script are preserved at commit
`2f39720aff59e98ee5143239b8a48a6a8bc8b3e2`.
