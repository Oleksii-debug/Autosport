# Portfolio-aware candidate optimization

Autosport separates bounded candidate generation from final portfolio-aware paper research ranking.

`BeamParlayCandidateSearch` is a combinatorial screening mechanism. Its `independent_probability` and `expected_profit_per_unit` assume independent candidate-leg probabilities and are not treated as proof of portfolio safety, correlation, or profitability.

`PortfolioAwareCandidateOptimizer` takes generated candidates, the current open paper tickets, a supplied canonical `ScenarioGroup` space and a hypothetical paper stake. It creates an in-memory synthetic `PaperTicket` for each candidate without mutating `PaperBook`, then compares the whole-portfolio scenario surface before and after the candidate.

## Marginal portfolio metrics

For each candidate the evaluator records the base and with-candidate scenario reports, observed worst/best-case changes, conservative floor/ceiling changes, expected-case change when available, exact/proven flags, existing tickets that share scenario groups with the candidate, and standalone expected profit only as a final tie-break signal.

The important distinction is between candidate P/L and change to the portfolio risk surface. A candidate can have the same standalone expected value as another candidate but materially improve or worsen the portfolio's worst-case exposure because of existing positions.

## Exact vs approximate truth

`observed_worst_case_change` is called proven only when both the base portfolio and the with-candidate portfolio have `worst_proven=true` from `ScenarioSearchEngine`.

When either minimum is unproven, the optimizer does not rank the observed minimum as an exact floor. Its risk component falls back to `conservative_floor_change`, and `ranking_risk_truth` is `conservative-floor-change`. The same distinction is preserved for best-case extrema.

Expected-case change retains the scenario engine's expected-value mode. Sampled independent-group expectation remains sampled/assumption-bound and is not relabeled exact.

## Outcome-independent truth

`OUTCOME_INDEPENDENT_POSITIVE` is a stronger mature-product truth label defined by GitHub Issue #355. It must **not** be inferred from this candidate optimizer's observed or expected metrics alone.

The label is permitted only for an exact executable position/stake vector after the complete relevant terminal outcome space has been proven and every terminal state has `net P&L > 0`, including applicable settlement rules, stake granularity, provider/account limits, quote freshness/slippage, fees/commission/tax, partial acceptance/rejection and execution sequencing assumptions.

If any required state or execution fact is incomplete, sampled, approximate, stale or otherwise unproven, use a weaker label such as `THEORETICAL_ARBITRAGE_ONLY`, `EXECUTION_RISK_PRESENT`, `PARTIAL_COVERAGE`, `HEDGED_BUT_NOT_GUARANTEED` or `RISKED_PORTFOLIO`. Exact-vs-approximate truth must survive candidate generation, portfolio planning and later execution planning without promotion.

## Scenario and candidate validation

Final portfolio-aware evaluation requires explicit `ScenarioGroup` definitions. It fails closed when an existing ticket or candidate quote is outside the scenario space, a quote appears in multiple scenario groups, one candidate contains two mutually exclusive outcomes from one group, a quote key is not canonical `event|market|selection`, or its declared event id disagrees with the quote key.

Candidate summary fields are also recomputed from the actual legs. `combined_odds`, `independent_probability`, and `expected_profit_per_unit` must exactly match the legs; forged or stale summary values are rejected before ranking. Leg probability must be in `[0,1]` and decimal odds must be greater than one.

The optimizer never invents missing correlation structure.

## Ranking order

Candidates are ranked deterministically by:

1. whether worst-case change is proven;
2. exact worst-case change, or conservative-floor change when not proven;
3. availability and value of expected-case change;
4. smaller existing dependency footprint as a tie-break;
5. standalone independent-probability expected profit only as the final tie-break.

This is a paper-research ranking surface. It does not mutate PaperBook, open a ticket, bypass `PaperRiskPolicy`, execute a bookmaker action, or claim that a positive expected value is profitable in live use.

`REAL_MONEY_EXECUTION=false` remains invariant.
