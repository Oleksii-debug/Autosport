# Probability quality on the consumed tennis sample

On the same 2,256 eligible 2025 tennis matches, score **all** predicted match
outcomes, including NO_BET decisions. The market proxy is normalized reciprocal
best-closing prices, not independently verified consensus or executable quotes.

| Forecast | Mean Brier error | Mean log loss |
|---|---:|---:|
| Fixed Elo | 0.229407 | 0.650345 |
| 75% market proxy / 25% Elo | 0.200008 | 0.584637 |
| Market proxy | 0.197656 | 0.576648 |

Lower scores are better. Elo forecasts are substantially less accurate on this
sample; the blend is better than Elo but still worse than the market proxy. This
helps explain why blindly selecting perceived Elo/price discrepancies is risky.
It does not prove a betting edge: these are already consumed historical tennis
data, best quotes can come from different books and times, and fill/result timing
and selection effects are unverified. Brier quality does not guarantee profit.

All forecasts, bets and NO_BET outcomes share the same denominator. Fourteen
focused tests passed including score calculation on abstained events and
financial cohort reconciliation. The source attribution and content hash remain
in `ACCOUNT_C_FULL_TENNIS_RESEARCH_REVIEW_20261028.md` and the result JSON.
No runtime model, money, risk, forward evidence or promotion authority changes.
