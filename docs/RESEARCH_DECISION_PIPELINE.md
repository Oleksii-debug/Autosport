# Typed research decision pipeline

Autosport separates research reasoning from deterministic paper execution.

The current V1 predictive research decision path is:

`ResearchEvidence -> ForecastRecord -> deterministic critic -> portfolio-aware impact -> PaperRiskPolicy -> PaperBook + Decision Ledger`

No LLM, network request or bookmaker write action exists inside this path.

This forecast-bound path is intentional for the present predictive paper journey; it is **not** a product-wide requirement that every mature strategy fabricate a directional forecast. GitHub Issue #356 defines the post-V1 generic opportunity contract: forecasting remains required for predictive/hybrid probability-edge intents, while pure arbitrage, dutching and some hedge/rebalance intents may instead be validated from causal executable quote structure plus complete terminal-state economics. The V1 pipeline is preserved as a predictive adapter rather than rewritten on the release path.

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

### Forecast

The existing immutable `ForecastRecord` remains the forecast contract for predictive intents. The critic requires causal timestamps and verifies that candidate probability comes from that record rather than an independently supplied number.

A future generic opportunity path must not synthesize a `ForecastRecord` merely to satisfy this interface. Strategy classes without a probability-edge claim must use their own bounded validator under #356 while preserving the same causal evidence, portfolio, risk and ledger authorities.

### Critic

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

For mature live opportunity classes, whole-portfolio authority remains mandatory even when forecast is absent. Arbitrage/dutching/hedge claims that use `OUTCOME_INDEPENDENT_POSITIVE` must satisfy Issue #355's complete-terminal-state and exact executable-plan contract, including stake vector, settlement semantics, limits/granularity, applicable costs, quote freshness/slippage and partial-acceptance/execution sequencing risk.

### Strategy / Audit

`ResearchDecisionPipeline.decide_and_open(...)` opens only a virtual PaperBook ticket when every critic, portfolio-policy and risk gate passes.

Both approvals and rejections are written as `DecisionRecord` entries with:

- causal decision timestamp;
- candidate identity and economics;
- critic verdict and leg-level reasons;
- forecast ids/hashes/versions/cutoffs;
- evidence ids/hashes/quality flags;
- portfolio risk deltas and truth label;
- PaperRiskPolicy result;
- deterministic pre-decision context hash;
- `real_money_execution=false`.

The later generic contract must preserve equivalent auditability while binding strategy class, exact event/market/selection/provider identities, source/receive timestamps, quote freshness, evidence/provenance hashes, portfolio identity, decision timestamp, strategy/model/config identity, exact/approximate/completeness truth and risk-policy result.

## Transaction boundary

When this pipeline is used inside a persistent dataset run, its PaperBook and Decision Ledger must be the staged objects owned by the run transaction introduced in PR #24. That gives the research decision the same early-crash atomicity as the rest of the paper run.

The pipeline itself does not create a cross-process lock and does not authorize multiple economic writers in one workspace.

## Non-claims

A green research decision means the supplied typed evidence, forecast, portfolio and risk policies allowed a paper action. It does not prove:

- forecast accuracy;
- profitability;
- independence of scenario groups;
- bookmaker fill/execution;
- legal right to retain or redistribute provider data;
- human/NVDA acceptance.

Real-money execution remains absent.
