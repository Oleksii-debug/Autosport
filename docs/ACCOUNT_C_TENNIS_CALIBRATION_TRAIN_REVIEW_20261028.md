# Trained calibration and the unverified-price finding

The retrospective input is the published minmaxxxer CC BY 4.0 tennis sample
described in `ACCOUNT_C_FULL_TENNIS_RESEARCH_REVIEW_20261028.md`. The source's
best closing prices and actual availability remain unverified; outcomes are
historical and have already been inspected. This is not an untouched holdout,
a verified fill, product promotion or evidence of real-money profitability.

The regularized market-anchored calibrator trained on 3,295 early eligible examples
and was evaluated on 2,256 later eligible matches. The estimated Elo coefficient
was -0.00716; its Brier score was 0.197977 versus 0.197656 for the unverified
market proxy. Lower is better. These are diagnostic forecasts only.

The original candidate selected 93 hypothetical one-unit tickets and obtained
+27.5264 units under the assumed fee, but **every selected ticket** relied on
two best quoted odds whose inverse probabilities sum to less than one. Across
the full evaluation, 548 of 2,256 source rows had this underround condition.
The two prices can come from different bookmakers and their simultaneity,
availability, limits and acceptance are not evidenced by this dataset. The
publisher describes closing prices with reference to the start of the match;
the independent capture time needed for the prediction is not verified.

A conservative post-hoc control abstained whenever the selected pair had this
unverified underround; it therefore placed **zero bets**, with ROI unavailable.
This does not prove the original quoted arbitrage never existed, and it does not
demonstrate a viable strategy. It does show why the apparent +27.5264 must not
be represented as earned money or deployed policy. Do not change runtime or
promote either variant without observed point-in-time, simultaneous executable
price and settlement evidence on a new evaluation population.

Seventeen fixture tests passed across training, source adaptation and economic
calculation. They cover same-day and evaluation-outcome isolation of training,
order invariance, costs and counting unverified underrounds as abstentions.
No independent source or live bookmaker verification is claimed.
