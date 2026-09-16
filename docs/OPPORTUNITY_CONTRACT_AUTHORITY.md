# Stage-C Opportunity Contract Authority Map

`autosport.opportunity` is a non-activating adapter contract. It gives later Stage-C work one canonical way to serialize and identify cross-strategy opportunities, bounded opportunity sets, and stake-vector plan snapshots without creating a second bankroll, risk, settlement, ledger, scheduling, or execution authority.

## What this seam owns

- Immutable `Opportunity`, `OpportunitySet`, and `PortfolioPlan` snapshot contracts.
- Stable canonical SHA-256 identities derived from serialization-safe contents.
- Deterministic ordering and duplicate/conflict rejection inside those snapshots.
- Explicit `WAIT`, `ZERO`, and `ACTIONABLE` opportunity classification.
- Strategy-class forecast requirements: predictive opportunities require causal forecast evidence for every referenced quote; non-predictive classes may legitimately omit forecasts.

## What this seam reuses and does not own

- `MarketEvent` owns structured event/market/selection identity, provider/source identity, sequence, quote odds, and observation facts. `QuoteRef` snapshots those facts and binds them to a hash of the canonical `MarketEvent` payload; it does not reinterpret them.
- `ForecastRecord` owns forecast probability, model/data cutoff, causal provenance, and forecast evidence. `ForecastRef` stores only a reference snapshot and canonical forecast hash.
- Candidate search/optimizer code owns candidate arithmetic and portfolio-impact calculations. Opportunity contracts do not recompute candidate economics.
- `PortfolioEngine` and scenario-search authorities own exact/conservative/approximate portfolio-risk truth. `EvidenceRef` is only an opaque reference to that evidence.
- `ProposedTicketRiskContext` owns typed proposed-ticket leg/quote/window identity at the executable paper-risk boundary; `EconomicGoalContract` and `PaperRiskPolicy` own owner limits and executable paper-risk allow/deny decisions. A plan cannot widen or replace those authorities.
- `PaperBook`, decision-ledger, settlement, and recovery code own bankroll, durable ticket/ledger state, lifecycle, settlement, and restart truth. `PortfolioPlan` does not mutate any of them.

An `EvidenceRef` proves only which external authority/evidence item the snapshot says it depended on. The reference itself is not an approval, verification, freshness proof, or execution grant. Any future runtime activation must revalidate the authoritative objects at the activation boundary.

## Positive-allocation boundary

A positive `PlanAllocation` is permitted only for an `ACTIONABLE` opportunity and only when the plan carries non-empty references for all three upstream evidence classes: portfolio analysis, risk decision, and ledger/PaperBook state. This is intentionally necessary-but-not-sufficient. The plan remains data only and cannot place a paper or real-money ticket.

`WAIT` and `ZERO` opportunities can appear in the same canonical opportunity set for complete decision evidence, but they cannot receive a positive stake. Zero allocations are allowed without pretending that upstream execution approval exists.

## Identity and serialization

All monetary/odds/probability values crossing this seam are exact `Decimal` values serialized as canonical decimal strings. Quote identity remains structured and includes provider/source plus sequence; ambiguous duplicate serialized quote keys are rejected rather than silently collapsed. Serialized IDs are verified on read, so payload tampering fails closed.

## Residual Stage-C work outside this seam

This contract does not implement opportunity generation, portfolio optimization or stake search, live-movement/lead-lag detection, arbitrage or dutching discovery, hedge/rebalance execution, bookmaker/provider integration, persistent live scheduling, or any paper/real-money placement path. Those future slices must reuse this seam plus their existing canonical data/risk/ledger authorities instead of creating competing identities or economic state.

Sport identity also remains outside this seam until the canonical #339 authority exists. Strategy/model promotion remains governed by the research/scientific protocol rather than by the presence of an `Opportunity` or `PortfolioPlan` object.

The seam has no GUI, Windows, bookmaker, scheduler, persistent-live-loop, F03 data mutation, paper placement, or real-money execution behavior. It does not change release truth: `REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, and `V1_READY=false` remain governed by their existing release/evidence authorities.
