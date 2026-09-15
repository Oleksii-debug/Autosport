# Typed research decision pipeline

## Scope of this document

The current V1 pipeline is a **predictive paper strategy path**, not the permanent definition of every mature Autosport opportunity.

Current V1 path:

`ResearchEvidence -> ForecastRecord -> deterministic critic -> portfolio-aware impact -> PaperRiskPolicy -> PaperBook + Decision Ledger`

This path remains valid for V1 and should not be rewritten merely to satisfy future architecture while release-critical work remains.

However, Autosport's mature strategy set also includes live price movement, arbitrage, dutching, hedging/rebalancing and hybrid portfolio strategies. Some of those do not require a directional winner forecast. The successor generic contract is tracked by Issue #356 and live economic truth by #355.

No LLM, network request or bookmaker write action exists inside the current V1 path.

## Typed role outputs

### Research / Data Quality

`ResearchEvidence` records:

- canonical quote key;
- source identity;
- observation time;
- time the evidence became available to the decision process;
- decimal odds;
- exact evidence content SHA-256;
- optional market snapshot SHA-256;
- explicit source/data-quality flags.

Evidence cannot become available before it was observed.

### Forecast — current predictive V1 strategy class

The immutable `ForecastRecord` remains the forecast contract for **predictive** V1 decisions. The critic verifies that candidate probability comes from that record rather than an independently supplied number.

A ForecastRecord is therefore mandatory when a strategy claims a predictive probability edge.

It is **not** a permanent global requirement for future pure arbitrage/dutching/hedging opportunities whose proof is price/portfolio based rather than directional.

### Critic — current predictive path

`DeterministicResearchCritic` verifies, per candidate leg:

- forecast exists for the same quote;
- forecast was generated no later than decision time;
- input cutoff is no later than decision time;
- candidate probability exactly matches ForecastRecord probability;
- uncertainty is within configured policy;
- a minimum number of evidence hashes used by the forecast were already available by its input cutoff;
- the forecast covers the latest evidence available at decision time;
- candidate odds match the latest evidence;
- blocked data-quality flags are absent;
- optional market snapshot hash matches forecast and latest evidence.

The default blocked flags include stale source, future clock skew, source-time regression, truncated batch and explicit gap detection.

### Portfolio / Risk

`PortfolioAwareCandidateOptimizer` evaluates the candidate against the existing whole paper portfolio on the supplied canonical `ScenarioGroup` space.

The decision keeps the optimizer's exact/approximate truth labels. Policy may require a proven exact worst-case change, but approximation is never relabeled exact.

`PaperRiskPolicy` is evaluated immediately before the paper ticket can be opened.

The whole portfolio is authoritative over a candidate considered in isolation.

### Strategy / Audit

`ResearchDecisionPipeline.decide_and_open(...)` opens only a virtual PaperBook ticket when every critic, portfolio-policy and risk gate passes.

Both approvals and rejections are written as `DecisionRecord` entries with:

- causal decision timestamp;
- candidate identity and economics;
- critic verdict and leg-level reasons;
- forecast ids/hashes/versions/cutoffs for this predictive path;
- evidence ids/hashes/quality flags;
- portfolio risk deltas and truth label;
- PaperRiskPolicy result;
- deterministic pre-decision context hash;
- `real_money_execution=false`.

## Transaction boundary

When this pipeline is used inside a persistent dataset run, its PaperBook and Decision Ledger must be the staged objects owned by the canonical run transaction. That gives the research decision the same early-crash atomicity as the rest of the paper run.

The pipeline itself does not create a cross-process lock and does not authorize multiple economic writers in one workspace.

## Mature generic opportunity successor — Issue #356

After V1, the mature decision shape should become strategy-class aware rather than forecast-mandatory:

`CausalOpportunityEvidence -> OpportunityIntent(strategy_class) -> strategy-specific validator -> whole-portfolio/min-P&L engine -> RiskPolicy -> PaperPlan/ExecutionPlan -> ledger`.

Minimum strategy classes:

- `PREDICTIVE_EDGE`;
- `LIVE_PRICE_MOVEMENT`;
- `ARBITRAGE`;
- `DUTCHING`;
- `HEDGE_REBALANCE`;
- `HYBRID`.

Shared evidence must bind event/market/selection/provider identity, causal timestamps, quote values/freshness, provenance, current portfolio, decision time, strategy/config identity and exact/approx/completeness truth.

Predictive/hybrid probability claims additionally require forecast origin/cutoff/probability/uncertainty/calibration evidence.

Pure arbitrage/dutching/hedge intents instead require the appropriate exact quote/state, settlement, stake-vector and complete terminal-state economic evidence when making an outcome-independent claim.

## Outcome-independent truth

The decision layer must not translate a positive expected value or a sampled portfolio surface into a guaranteed-profit claim.

`OUTCOME_INDEPENDENT_POSITIVE` is allowed only when #355's complete-terminal-state contract is satisfied and, before real money moves, #353 also proves exact execution feasibility.

## Non-claims

A green current V1 research decision means the supplied typed evidence, forecast, portfolio and risk policies allowed a **paper predictive action**. It does not prove:

- forecast accuracy;
- profitability;
- independence/completeness of scenario groups beyond what was proven;
- live quote executability;
- bookmaker fill/execution;
- legal right to retain or redistribute provider data;
- human/NVDA acceptance.

Real-money execution is currently absent/disabled. That is a current implementation fact, **not** the mature product boundary.

`REAL_MONEY_EXECUTION=false`
`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`V1_READY=false`
