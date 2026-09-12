# Автоспорт — canonical technical project

Status: living binding technical project. Whole-product development; release versions are checkpoints, not isolated rewrites.

## 1. Product definition

Автоспорт is an independent Windows multi-agent laboratory for sports-market observation, high-frequency odds history, causal historical replay, paper bankroll/tickets, portfolio exposure mathematics, forecasting, strategy evaluation and adaptive learning. The first vertical slice is table tennis, but identifiers and contracts are sport-agnostic so additional sports do not require rebuilding the core.

The product is deliberately independent from unfinished Nika-Core. We selectively reuse proven neutral code/components when useful, pin provenance and adapt only the smallest required boundary. Autosport must remain runnable even if Nika-Core changes or is unavailable.

## 2. Product goals

The user must be able to launch a packaged Windows application, navigate it entirely by keyboard/NVDA, inspect and copy all primary text, load a historical dataset or permitted live-observation feed, run causal replay at configurable speed, create/configure virtual bankroll experiments, run one or more strategies/agents, observe virtual singles/parlays and total portfolio exposure, settle finished events, compare strategy versions and resume durable experiments after restart.

Success is measured by reproducibility, low-latency reaction, mathematical correctness, causal integrity, accessibility and useful experimental throughput — not by number of agents or PRs.

## 3. Safety/scope boundary

Implemented execution is paper/simulation only. Live mode may observe and analyze permitted data and prepare human-review decisions, but the canonical product has no autonomous real-money wager execution path. Providers may not bypass authentication controls, CAPTCHAs, rate limits or contractual restrictions.

## 4. Architectural layers

### 4.1 Provider/ingestion layer

Each provider implements a narrow adapter that emits normalized events without exposing provider-specific HTML/JSON details to the rest of the system. A provider is responsible for source identity, timestamp capture, match/market/selection mapping, decimal odds parsing, score/status parsing where supported, reconnect/backoff and deduplication identity. Provider data is considered untrusted input and validated before entering canonical state.

Canonical hot path: `source -> provider adapter -> MarketEvent -> event bus/store -> current-state projection -> affected dependency graph -> calculations`. LLM calls are forbidden in this per-update path.

### 4.2 Canonical domain model

Core identities: `Sport`, `Competition`, `Participant`, `Match`, `Market`, `Selection`, `MarketEvent`, `ResultEvent`, `PaperAccount`, `Ticket`, `TicketLeg`, `Settlement`, `StrategyDecision`, `Experiment`, `ReplayRun`, `StrategyVersion`.

`MarketEvent` minimally binds event id/sequence, source/provider, observed-at UTC timestamp, optional source timestamp, match id, market id, selection id, event kind, decimal odds/value, status and metadata. Money/odds at ledger boundaries use decimal arithmetic.

Market types are extensible. Initial semantic families: match winner/moneyline, totals, handicap/spread, set/game winner and set/game totals/handicaps. Provider-specific labels map into canonical families plus opaque metadata when no canonical semantic exists yet.

### 4.3 Event store and current projection

The event store is append-only and immutable by default. Corrections are new events, not silent rewrites. A bounded current-state projection maintains latest values needed for live operation. Persistent historical storage should support high-rate append and efficient time-range/selection queries; initial research favors DuckDB/Parquet plus Polars for analytical pipelines, while the canonical interface remains storage-agnostic.

Ordering uses provider sequence when available and deterministic tie-break rules around observed timestamps. Duplicate event identity must be idempotent. Clock/timestamp provenance is retained so provider time and local receive time are distinguishable.

### 4.4 Replay engine

`MarketEventStream` is the single consumer-facing stream abstraction. Live and replay producers feed the same downstream contracts.

Replay modes: real-time, speed multiplier, event-driven/max-speed and deterministic stepped mode. The strategy surface cannot access events later than replay clock. Final results/settlement are withheld until their event time. Experiments persist dataset identity, replay configuration, seed where relevant, strategy version and engine version for reproduction.

Replay supports pause/resume, checkpoint/restart, deterministic ordering, seeking only in analysis mode, and clean isolation between training/evaluation windows. Seeking must never leak future information into a run that claims causal evaluation.

### 4.5 PaperBook and ledger

A paper account has base currency and initial bankroll. Ticket creation records immutable quoted odds, stake, legs, source timestamps, strategy/agent decision id and portfolio snapshot reference. Singles and parlays share one ledger model. Funds/exposure rules are explicit rather than inferred by an LLM.

Settlement supports win/loss/void/refund and future provider-rule extensions. Settlement is idempotent, auditable and cannot mutate the original decision record. Account balance, reserved stake, realized P&L and unrealized/exposure views are derived from ledger facts.

### 4.6 Portfolio and scenario engine

The portfolio engine tracks dependency edges from selections/outcomes to ticket legs and tickets. A market update invalidates only the affected dependency subgraph. Required outputs include committed stake, open exposure, scenario P&L, worst/best outcome over a defined scenario set, expected P&L when calibrated probabilities exist, concentration, drawdown and risk-of-ruin estimates.

The engine must not brute-force arbitrary `2^N` spaces. It supports a portfolio of algorithms behind stable interfaces: factor/dependency graphs, dynamic programming, branch-and-bound, dominance pruning, constraint/CP/MIP style exact solving where useful, scenario compression and Monte Carlo/sampling for very large spaces. Approximate output is labeled approximate; a claim such as `worst_case_profit >= 0` requires an exact or formally bounded result over the declared scenario universe.

Candidate ticket generation is separated from portfolio evaluation so different search algorithms can be benchmarked against the same evaluator.

### 4.7 Probability/forecast layer

Forecast components may combine statistical models, calibrated ML and AI-agent research. They emit probabilities with provenance, calibration version and input horizon. The portfolio engine consumes numeric probabilities; it never asks an LLM to perform basic arithmetic.

Implied probabilities, overround normalization, calibration transforms, correlation assumptions and uncertainty are explicit deterministic functions. Forecast quality is independently evaluated using Brier score/log-loss/calibration and out-of-sample windows.

### 4.8 Multi-agent laboratory

The product supports multiple independent roles over shared canonical data rather than isolated duplicate databases. Initial roles may include Research Agent, Match/Participant Analyst, Forecast Agent, Ticket Constructor, Portfolio/Risk Agent, Critic/Adversarial Agent and Learning/Evaluation Agent. A coordinator can spawn tasks/agents, but every material decision is persisted with role, model/provider, prompt/config/version and data horizon.

Agents communicate through typed records/events and durable task state. Agent failure must not corrupt hot-path market capture. Agent workloads are cancelable and restartable. Local/API model integration will use a provider-agnostic gateway; no single global model lock may unnecessarily serialize independent agents.

### 4.9 Learning/evaluation

Every strategy decision is recorded before outcome reveal. Learning data binds exact causal input horizon, probabilities, candidates, selected/rejected actions, bankroll/risk snapshot and strategy version. Post-settlement evaluation can explain where performance came from without allowing future data into the original decision.

Metrics include bankroll curve, ROI, realized/unrealized P&L, maximum drawdown, volatility, risk of ruin, hit rate only where meaningful, Brier/log-loss/calibration, performance by sport/market/pre-match/live/favorite/underdog/single/parlay, walk-forward/out-of-sample results and sensitivity after removing largest isolated wins.

Promotion of a strategy requires repeated out-of-sample evidence, not one profitable week or one high-payout parlay.

### 4.10 Windows/WebView application

Autosport uses a web-style semantic UI embedded in a Windows desktop shell, following the useful architectural pattern already explored in Accessible Chess. Python domain services expose bounded APIs to the UI; the DOM remains ordinary HTML rather than an accessibility-only virtual surface.

Primary routes/panels: Home/experiment status; Live/Replay market browser; Match detail/history; PaperBook/tickets; Portfolio/scenarios; Agents/tasks; Strategy/evaluation; Data/providers; Settings/diagnostics.

Navigation uses `header/nav/main/section`, H1/H2/H3, real buttons/links/forms/tables/lists, stable focus management and keyboard operation. Data refresh must preserve user focus/selection where possible instead of re-rendering the entire document destructively.

### 4.11 Copyable accessibility contract

Every primary fact, explanation, table cell, ticket, portfolio metric, error and result spoken by NVDA is also rendered as ordinary visible DOM text that the user can select and copy. Live regions contain only concise duplicate announcements such as `Odds updated` or `Replay paused`; they are never the sole owner of useful content. No global `user-select:none`; no CSS/JS trick that paints text while hiding it from selection; no interception of Ctrl+A/C/X/V/Z/Y in ordinary/standard editable contexts.

Automated tests inspect HTML/JS for this contract, but physical Windows/NVDA acceptance is a separate release gate and cannot be claimed by CI.

## 5. Persistence and data layout

Logical stores are separated even if initial implementation shares one process: configuration/profile store; append-only market event store; current market projection; paper ledger; experiment/replay checkpoints; agent/task records; strategy/evaluation records. Raw provider payload retention is configurable and must avoid secrets/private session data.

Historical analytics should support partitioning by provider/date/sport/competition/match and columnar export such as Parquet. Exact dataset manifests/hashes are retained for reproducibility.

## 6. Concurrency/performance principles

Ingestion, persistence, projection, mathematical recomputation and agent reasoning are decoupled with bounded queues/backpressure. Market capture has higher priority than slow AI work. An overloaded agent must not block capture. Event consumers are idempotent where possible. Expensive portfolio calculations are cancellable/supersedable when fresher events make an old calculation obsolete.

Initial engineering targets are measurement targets, not fabricated guarantees: ingest thousands of normalized events/second on a normal Windows laptop in synthetic benchmarks; update one affected selection/projection in milliseconds; keep UI responsive under background replay; accelerate historical replay until CPU/storage rather than artificial sleeps becomes the limit. Benchmarks will set real thresholds after measurement.

## 7. Reuse strategy

Before custom implementation, evaluate Nika-Core modules for model gateway, multi-agent/task persistence, memory and interaction contracts; Accessible Chess for pywebview/Windows shell, semantic keyboard/NVDA patterns, packaging and release validation; and mature libraries such as DuckDB, Polars and suitable optimization/statistics packages. Reuse is selective and provenance/license tracked. Autosport never takes a runtime dependency on unfinished Nika-Core for a critical capability.

## 8. Verification

Core gates: domain/schema tests; ordering/idempotency tests; causal replay/future-leakage adversarial tests; Decimal ledger/settlement tests; portfolio exact-small-case oracle tests; approximate-vs-exact benchmark tests; incremental invalidation tests; persistence/restart tests; provider fixture tests; accessibility/copyability static and browser tests; Windows packaged smoke; performance benchmark suite; deterministic release manifest/hash checks; physical NVDA acceptance for release.

Synthetic tests must never be misreported as real-provider or human evidence.

## 9. Release milestones while developing the whole product

`v0.1` runnable vertical slice: Windows/web-style shell, canonical MarketEvent, fixture/file provider, causal replay, current market view, virtual bankroll, basic single/parlay tickets, deterministic settlement, initial agent interface, persistence skeleton and accessibility/copyability regressions.

`v0.2` market/data scale: high-frequency persistent event store, richer market taxonomy, provider adapter framework, odds history, incremental dependency graph, portfolio exposure/worst-best calculations, accelerated replay and recovery.

`v0.3` intelligence lab: model gateway, multi-agent roles, forecast/calibration, candidate construction, critic/risk loop, strategy versioning, evaluation dashboard, walk-forward experiments and large replay campaigns.

`v0.4` optimization/resilience: scalable combinatorial solvers, Monte Carlo/approximation contracts, performance/backpressure hardening, live-observation adapters, long-run unattended experiments, robust Windows recovery and diagnostics.

`v1.0` production-grade paper laboratory: deterministic packaged Windows release, mature multi-sport providers where lawful, reproducible datasets/experiments, robust portfolio engine, agent orchestration, learning/evaluation, stable keyboard UI and physical NVDA acceptance evidence.

Work may advance v0.3/v0.4 foundations before v0.1 release if it shortens the whole path and does not destabilize the current runnable slice.

## 10. Immediate implementation order

1. Establish repository contracts, CI, semantic/copyable UI shell and core domain types.
2. Implement deterministic in-memory projection plus causal replay fixtures and adversarial leakage tests.
3. Implement paper ledger/tickets/settlement with Decimal arithmetic.
4. Establish persistence interface and benchmark candidate stores (DuckDB/Parquet/Polars) before locking implementation.
5. Implement dependency graph and exact small-case portfolio oracle, then incremental recomputation.
6. Add provider adapter contract and first table-tennis dataset/provider fixture; separate live permitted connector from historical replay.
7. Add agent/model gateway interfaces and first strategy/critic/evaluation loop.
8. Package Windows candidate early and continuously rather than postponing desktop integration to the end.
9. Expand markets, solvers, performance and providers under continuous regression/performance testing.

## 11. Definition of done

Whole product reaches 100% only when an exact packaged Windows candidate can be installed/extracted and launched, all critical flows work from that package, historical/live-observation streams feed the same contracts, causal replay is proven, ledger and portfolio math are validated, agents can run/recover, experiments persist/reproduce, performance gates pass, primary UI text remains selectable/copyable, and a physical Windows+NVDA acceptance is recorded. Until then progress is reported conservatively in `docs/PROGRESS.md`.
