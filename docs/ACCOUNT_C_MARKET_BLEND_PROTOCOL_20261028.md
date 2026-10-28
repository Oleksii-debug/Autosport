# Post-hoc market-anchored Elo challenger

The first full-file analysis showed 1,739 of 1,770 Elo bets were on underdogs and
that cohort lost 123.2542 hypothetical units after assumed costs. This observation
does not establish the cause, and the dataset has already been evaluated.

Test one fixed research-only alternative on that same consumed dataset: derive
the normalized market probability `q_a=(1/odds_a)/(1/odds_a+1/odds_b)` from the
two reported best closing odds, then `p_a=0.75*q_a+0.25*p_elo`. Require both
players to have at least one strictly earlier completed result, and use the same
0.05 probability edge threshold, 1-unit stake and assumed 2% winning-profit fee.
All other baselines and the exact evaluation denominator remain unchanged.

The two best prices can come from different books and may not have been available
at the same time. Their timestamps and actual execution are unverified, and
same-day result publication is assumed rather than verified. Because this design
was selected after seeing the results and the data are already consumed, output
is exploratory, not untouched holdout, forward evidence or a profitability claim.
Do not promote the candidate, edit runtime memory or expand risk limits.
