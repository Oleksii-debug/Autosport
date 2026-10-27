# Account C: exploratory league-conditioned confidence

This is experiment TWO, chosen after seeing experiment ONE. The same 2,000-row
sample is already consumed development data; it is not relabeled a fresh holdout.
The first result and its script remain pinned at GitHub commit
`2f39720aff59e98ee5143239b8a48a6a8bc8b3e2` and are not overwritten.

Observed motivation: Czech Liga Pro remained worse than constant 0.5 under a
global confidence adjustment; a global aggregate obscures differences by league.

Freeze one new challenger, with no parameter search:

- Reuse all rows, the 28-day warmup, all four original controls and the same
  evaluation denominator from experiment one.
- Compute the global slope from strictly earlier dates exactly as before.
- For each league, compute `clip((xy_league + global_slope)/(xx_league + 1),0,1)`.
  The ridge constant 1 shrinks sparse league evidence toward the global slope.
  An unseen league receives exactly the global slope; no handpicked league list.
- `xy_league=sum((p-.5)*(y-.5))`; `xx_league=sum((p-.5)^2)`. Update both only after
  predicting ALL matches on the current date. Repeated players and source sample
  selection are not treated as independent observations.
- Evaluate Brier and log loss on all 1,503 later rows, plus all weeks and leagues.
  Retain regressions and the global baseline. Do not tune constants after seeing
  results. Any future alteration is a new experiment.

This remains a read-only research diagnostic. Publisher pre-match and outcome
assertions are unverified. No odds/costs/fills exist; no money/ROI/promotion claim,
runtime update, production memory import or source-rights issuance is allowed.
An apparent improvement would require a separately frozen prospective evaluation
with authenticated data before any product promotion.
