# Recovery across both families: exploratory development outcome

This follow-up was chosen after inspecting the 40-step candidate; the three
fixtures are previously seen development data and cannot be a holdout. All arms
use identical snapshots and PAPER accounting. Reward comes from synthetic quote
movement, not actual match outcomes or verified execution.

| Scenario | Current rule net | 40-step net | 37-step net | 37-step drawdown |
|---|---:|---:|---:|---:|
| Persistent loss | -1468 | -1636 | -1594 | 1594 |
| Favorable | 160534.32 | 160534.32 | 160534.32 | 0 |
| Loss then recovery | -1468 | 26728.9984 | 27133.1472 | 1510 |

The 37-step candidate reached both SINGLE and PARLAY in the persistent-loss
fixture. It incurred three additional tickets and a further 126 UAH synthetic
loss against the current rule; in the recovery fixture it eventually restarted
trading. Scheduled probes are bounded, but normal exploitation after a positive
reward is not bounded by the number of probes. Large positive figures result from
the artificial payout model and do not establish profitability.

Neither candidate is promoted or connected to runtime memory. This follow-up
does not prove statistical robustness, real-world prediction or an economic edge.
Next qualification requires new data, matched actual results, decision-time prices,
costs, risk limits and an untouched evaluation. The original 40-step script and
result remain available at commit `03d23f398d11f2f9e050108cf6fb7a627c1b8b0f`.
