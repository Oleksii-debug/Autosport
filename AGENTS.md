# AGENTS.md — Autosport autonomous development contract

## Product identity

Repository: `Oleksii-debug/Autosport`.
Product name: **Автоспорт / Autosport**.
Autosport is an independent Windows multi-agent sports-market laboratory. It must not wait for Nika-Core to become finished. Reuse from Nika-Core, Accessible Chess and lawful open-source projects is encouraged only when it reduces time-to-product and the borrowed boundary can be understood, tested and owned here.

## Primary objective

Optimize `TIME_TO_USEFUL_AUTOSPORT`, not PR count, code volume, agent count or documentation volume. Every substantial run should either advance a user-visible product capability, remove a concrete blocker, converge/reuse proven work, improve verification/performance, or prepare an integration-ready bounded change.

Development is whole-product. Version labels are release checkpoints, not walls between teams. Work may advance ingestion, replay, portfolio mathematics, agents, UI, persistence, evaluation and packaging in parallel when ownership is disjoint.

## Canonical product boundaries

Autosport currently implements paper/simulation, historical replay, live-observation/analysis, forecasting, portfolio/risk and human-review decision support. Do not add an autonomous real-money wagering executor or bookmaker-clicking flow. Do not bypass site access controls, CAPTCHAs, rate limits or contractual restrictions. Providers must be lawful, configurable adapters over permitted/public/user-authorized data.

## Reuse-first rule

Before implementing a non-trivial subsystem, search in this order: current Autosport -> Nika-Core -> Accessible Chess -> mature open-source libraries/projects -> custom code. Record reusable candidates in `docs/REUSE_MATRIX.md`. Prefer `REUSE -> ADAPT -> THIN CUSTOM` and avoid wholesale forks. Never copy code with unclear licensing/provenance.

## One canonical implementation per capability

Do not create competing event stores, replay engines, bankroll ledgers, portfolio engines, model gateways, agent runtimes or UI shells. If an incumbent exists, repair/extend it. New implementation requires a documented reason and migration plan.

## Hot-path invariant

Per-odds-update processing must not require an LLM call. The canonical hot path is: provider -> deterministic parser/adapter -> normalized `MarketEvent` -> current-state projection/event store -> incremental mathematics. AI agents consume structured state above this path.

## Causal replay invariant

Historical strategy execution must never receive future events, final outcomes or settlement data before replay time reaches them. Replay input may be known to the engine internally, but the strategy-facing interface exposes only causally released events/state. Every evaluation record must bind decision time, data horizon and strategy/model version.

## Numerical invariant

Money and decimal odds use exact decimal arithmetic at ledger boundaries. Do not use binary float for bankroll/stake/settlement amounts. Approximate solvers/Monte Carlo must label approximation/error assumptions; guaranteed or worst-case claims require exact/formally bounded evidence over the defined scenario space.

## Accessibility/copyability invariant

Autosport is keyboard/NVDA-first. Any primary content spoken by NVDA must also exist as ordinary visible, selectable, copyable DOM text. `aria-live`, `role=status`, `role=alert` and visually hidden nodes may announce short duplicate notifications, but may never be the only representation of market data, strategy explanation, ticket state, portfolio state, errors or results. Do not apply global `user-select:none`. Do not intercept standard Ctrl+A/C/X/V/Z/Y in ordinary text/editable contexts. Navigation uses semantic HTML headings, landmarks, lists/tables and predictable focus.

No agent may claim `NVDA_VERIFIED=true` or `HUMAN_TESTED=true` without a physical Windows+NVDA human run tied to an exact candidate SHA.

## Performance architecture

Assume tens of simultaneous matches, thousands of selections/events, large historical streams and combinatorial portfolios. Prefer append-only events, bounded in-memory current-state projections, batch/columnar historical analytics and incremental dependency invalidation. Never recompute an entire portfolio merely because one selection changed when affected dependencies can be identified.

## Testing

Every bounded capability receives deterministic tests. Core tests must cover ordering/idempotency, future-leakage resistance, exact ledger arithmetic, settlement invariants, restart/persistence as introduced, and accessibility/copyability contracts. CI should run on Windows and Linux where practical. Queued CI is not failure; unchanged-head reruns are not product progress.

## Git workflow

Do not write feature work directly to `main`. Use bounded branches and PRs. Bootstrap repair on main is historical exception only. Re-read live main/open PRs before creating a successor. One writer per production slice. Prefer integration/convergence of existing useful work over spawning duplicate PRs.

## Reporting

Maintain `docs/PROGRESS.md`. Progress is evidence-based and conservative. At the end of a meaningful run report: product movement, changed files/PR, tests, current blocker, next action, and whole-product percent. Never inflate percentage because documentation or scaffolding exists.
