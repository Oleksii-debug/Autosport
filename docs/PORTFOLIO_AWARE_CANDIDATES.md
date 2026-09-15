# Portfolio-aware candidate optimization

Autosport separates bounded candidate generation from final whole-portfolio economic evaluation.

The current V1 `BeamParlayCandidateSearch` is a combinatorial screening mechanism. Its `independent_probability` and `expected_profit_per_unit` assume independent candidate-leg probabilities and are **not** proof of portfolio safety, correlation, live executability or profitability.

`PortfolioAwareCandidateOptimizer` takes generated candidates, the current open paper tickets, a supplied canonical `ScenarioGroup` space and a hypothetical paper stake. It creates an in-memory synthetic `PaperTicket` for each candidate without mutating `PaperBook`, then compares the whole-portfolio scenario surface before and after the candidate.

This current paper implementation is a foundation for the mature live/outcome-independent portfolio program in Issue #355. It must not be interpreted as permanently paper-only.

## Marginal portfolio metrics

For each candidate the evaluator records the base and with-candidate scenario reports, observed worst/best-case changes, conservative floor/ceiling changes, expected-case change when available, exact/proven flags, existing tickets that share scenario groups with the candidate, and standalone expected profit only as a final tie-break signal.

The important distinction is between candidate P&L and change to the portfolio risk surface. A candidate can have attractive standalone expected value and still worsen the portfolio's minimum terminal P&L because of existing positions. Conversely, a hedge/dutching leg can have weak standalone expected value while materially improving the whole portfolio.

Mature Autosport therefore treats the full open + proposed portfolio and stake vector as the economic decision object.

## Exact vs approximate truth

`observed_worst_case_change` is called proven only when both the base portfolio and the with-candidate portfolio have `worst_proven=true` from `ScenarioSearchEngine`.

When either minimum is unproven, the optimizer does not rank the observed minimum as an exact floor. Its risk component falls back to `conservative_floor_change`, and `ranking_risk_truth` is `conservative-floor-change`. The same distinction is preserved for best-case extrema.

Expected-case change retains the scenario engine's expected-value mode. Sampled independent-group expectation remains sampled/assumption-bound and is not relabeled exact.

Sampling/Monte Carlo may estimate downside or expected behavior. It must never be used to claim guaranteed/outcome-independent positive P&L on an incomplete terminal-state space.

## Outcome-independent positive contract

The mature product may label a position set `OUTCOME_INDEPENDENT_POSITIVE` only when the complete relevant terminal-state space is proven and the exact **executable** plan has `minimum terminal net P&L > 0` after all applicable economic/execution constraints.

That final proof belongs to the combined #355 portfolio + #353 execution contract and must include, where relevant:

- exact actionable odds/quote freshness;
- provider/bookmaker settlement semantics;
- stake granularity;
- minimum/maximum stakes and payout limits;
- fees/commission/tax;
- slippage;
- partial acceptance/rejection;
- execution ordering/atomicity assumptions;
- current real open positions.

A mathematically positive paper surface may therefore be classified only as `THEORETICAL_ARBITRAGE_ONLY` or another weaker truth label until execution feasibility is proven.

## Scenario and candidate validation

Final portfolio-aware evaluation requires explicit `ScenarioGroup` definitions. It fails closed when an existing ticket or candidate quote is outside the scenario space, a quote appears in multiple scenario groups, one candidate contains two mutually exclusive outcomes from one group, a quote key is not canonical `event|market|selection`, or its declared event id disagrees with the quote key.

Candidate summary fields are recomputed from the actual legs. `combined_odds`, `independent_probability`, and `expected_profit_per_unit` must exactly match the legs; forged or stale summary values are rejected before ranking. Leg probability must be in `[0,1]` and decimal odds must be greater than one.

The optimizer never invents missing correlation or terminal-state structure.

## Ranking order — current V1 predictive paper surface

Current candidates are ranked deterministically by:

1. whether worst-case change is proven;
2. exact worst-case change, or conservative-floor change when not proven;
3. availability and value of expected-case change;
4. smaller existing dependency footprint as a tie-break;
5. standalone independent-probability expected profit only as the final tie-break.

This current ranking surface does not mutate PaperBook, open a ticket, bypass `PaperRiskPolicy`, execute a bookmaker action, or prove live profitability.

## Mature strategy extension

After exact V1 release, #356 introduces a generic opportunity contract so portfolio evaluation can consume strategy classes beyond forecast-bound candidate generation:

- predictive edge;
- live price movement;
- arbitrage;
- dutching;
- hedge/rebalance;
- hybrid.

Forecast probability remains required for a strategy that claims predictive edge. It is not globally mandatory for an arbitrage/dutching/hedging opportunity whose economic proof comes from causal executable quotes and complete terminal-state P&L.

`REAL_MONEY_EXECUTION=false` is the current implementation truth. It is **not** a permanent product invariant.
`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`V1_READY=false`
