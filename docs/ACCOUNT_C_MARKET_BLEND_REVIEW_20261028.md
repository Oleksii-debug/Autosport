# Post-hoc market-anchored Elo: development result

This follows the already reviewed 5,791-row minmaxxxer CC BY 4.0 tennis data,
with 3,297 early matches for Elo updates and the same 2,256 eligible later
matches. Best closing prices and outcomes are publisher assertions, not verified
executed fills. Data were reused after seeing the first results; the design is
post-hoc and not an untouched holdout.

| Policy | Hypothetical bets | Net units | ROI | Drawdown units |
|---|---:|---:|---:|---:|
| Plain Elo | 1770 | -125.8576 | -7.11% | 170.8736 |
| Elo plus player-history guard | 1616 | -113.0990 | -7.00% | 157.5060 |
| Market-anchored Elo plus history guard | 443 | -16.3126 | -3.68% | 61.0386 |
| Market favorite control | 2256 | +13.3260 | +0.59% | 37.2392 |

One-unit bets and a hypothetical 2% fee on winning profit are assumed. The blend
reduced losses and the number of bets on this consumed sample, but remained
negative. The favorite control's small positive result is not proof of edge.
The blend made 411 underdog bets for -38.0858 units and 32 favorite/tie bets for
+21.7732 units; small cohorts can be unstable and were not used to filter results.

The unguarded Elo chose 1,739 underdogs for -123.2542 units and 31 favorites/ties
for -2.6034 units. These descriptive cohorts identify where losses occurred, but
do not establish causal reasons. The two best closing odds can come from different
books at unknown times, and no actual settlement or available stake is verified.
Drawdown is match-start-ordered. Tennis is not table tennis; no future profitability,
market execution, statistical significance or promotion claim is made.

Thirteen focused tests passed, including financial cohort reconciliation,
same-day causality, unknown player handling and prevention of tickets from weak
market disagreement. Both original full-file and unfavorable results remain
available in commit `79eed0ac1c78c4d6362e75d657967558585620d2`.
