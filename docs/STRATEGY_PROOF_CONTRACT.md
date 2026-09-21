# Strategy proof contract (WP-S08)

`autosport.strategy_proof_contract` is the additive proof-taxonomy seam for the
Stage-C generic opportunity language described by #355, #356, #362 and #763.

It does **not** replace the current predictive pipeline, EconomicGoal,
RiskPolicy, portfolio/scenario engine, execution ledger, provider adapters, or
settlement authority. It has no network or money-moving behavior.

## Strategy families

The contract distinguishes:

- `PREDICTIVE_EDGE`
- `LIVE_PRICE_MOVEMENT`
- `ARBITRAGE`
- `DUTCHING`
- `HEDGE_REBALANCE`
- `HYBRID`
- `WAIT`

Every family binds the shared causal/identity/freshness/portfolio/decision-time
truth classes. Family-specific proof classes then prevent relabelling one kind
of evidence as another.

Predictive and hybrid families require forecast probability, causal forecast
provenance, and uncertainty/calibration evidence. Pure live-price movement does
not require a directional forecast. Arbitrage and dutching require executable
quote-set, complete terminal-state, stake-vector, settlement, cost, limit,
granularity, and minimum-terminal-net-P&L evidence classes. Hedge/rebalance
requires existing exposure and residual-risk improvement evidence rather than a
forecast.

## Fail-closed semantics

`evaluate_strategy_proofs()` compares a `frozenset[ProofRequirement]` with the
canonical family contract.

- missing required proof -> `positive_action_candidate=False`;
- missing proof -> `fallback_family=WAIT`;
- `WAIT` is never a positive-action family;
- `execution_authorized` is always `False`.

A fully satisfied non-WAIT proof taxonomy means only that the required evidence
**classes are present**. Their content still has to pass the canonical
validators, whole-portfolio economics, RiskPolicy, execution feasibility,
provider reconciliation, and every other downstream authority.

In particular, the contract cannot produce `OUTCOME_INDEPENDENT_POSITIVE` by
itself. That truth still requires the complete executable-state proof defined
by #355.

## Determinism and anti-bypass

Contracts and evaluations are frozen dataclasses. Required/present/missing proof
tuples use canonical lexical ordering. Callers cannot construct a shortened
canonical family contract or set `execution_authorized=True`.

This slice intentionally contains no adapter into `ResearchDecisionPipeline`;
that future integration should adapt the proven predictive path into the
generic opportunity language rather than creating a second economic truth.
