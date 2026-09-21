# Strategy proof contract (WP-S08)

`autosport.strategy_proof_contract` is the additive proof-taxonomy seam for the
Stage-C generic Opportunity language described by #355, #356, #362 and #763.

It does **not** replace the current predictive pipeline, EconomicGoal,
RiskPolicy, portfolio/scenario engine, execution ledger, provider adapters, or
settlement authority. It has no network or money-moving behavior.

## Canonical identity only

This module reuses the canonical `StrategyClass` and `OpportunityDecision` from
`autosport.opportunity`. It deliberately creates no second strategy vocabulary
and no second decision identity.

Canonical strategy classes remain:

- `PREDICTIVE_EDGE`
- `LIVE_PRICE_MOVEMENT`
- `ARBITRAGE`
- `DUTCHING`
- `HEDGE_REBALANCE`
- `HYBRID`

`WAIT` is **not** a strategy class. It remains the canonical
`OpportunityDecision.WAIT` fail-closed decision state.

Every strategy class binds shared causal/identity/freshness/portfolio/
decision-time truth classes. Class-specific proof obligations then prevent
relabeling one kind of evidence as another.

`PREDICTIVE_EDGE` requires a probability-edge claim and forecast probability,
causal forecast provenance, and uncertainty/calibration evidence. Pure
`LIVE_PRICE_MOVEMENT` does not require a directional forecast. `ARBITRAGE` and
`DUTCHING` require executable quote-set, complete terminal-state, stake-vector,
settlement, cost, provider-limit, granularity, and minimum-terminal-net-P&L
evidence classes. `HEDGE_REBALANCE` requires existing-exposure and
residual-risk-improvement evidence rather than forecast evidence.

`HYBRID` follows the already-canonical Opportunity rule: its
`claims_probability_edge` flag may be false or true. Forecast proof obligations
are added only when that canonical flag is true; hybrid-component evidence is
always required.

## Fail-closed semantics

`evaluate_strategy_proofs()` compares a `frozenset[ProofRequirement]` with the
canonical contract for `(StrategyClass, claims_probability_edge)`.

- missing required proof -> `positive_action_candidate=False`;
- missing proof -> `proof_gate_decision=OpportunityDecision.WAIT`;
- complete proof classes -> proof gate may return `OpportunityDecision.ACTIONABLE`;
- `execution_authorized` is always `False`.

`proof_gate_decision=ACTIONABLE` is only the result of this narrow proof-class
presence gate. It is not the final Opportunity decision and is not permission
to place a stake. Evidence contents still have to pass their canonical
validators, whole-portfolio economics, EconomicGoal, RiskPolicy, execution
feasibility, provider reconciliation, settlement authority, and every other
binding downstream gate.

In particular, this contract cannot produce `OUTCOME_INDEPENDENT_POSITIVE` by
itself. That economic truth still requires the complete executable-state proof
defined by #355 and the existing EconomicGoal authority.

## Determinism and anti-bypass

Contracts and evaluations are frozen dataclasses. Required/present/missing proof
tuples use canonical lexical ordering. Callers cannot construct a shortened
canonical class contract, inject predictive claims into a non-predictive class,
or set `execution_authorized=True`.

This slice intentionally contains no adapter into `ResearchDecisionPipeline`;
a future integration should adapt the proven predictive path into the canonical
Opportunity language rather than creating a second economic truth or execution
path.
