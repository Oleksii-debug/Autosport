# АВТОСПОРТ — MASTER TECHNICAL PROJECT

## 1. Продуктова мета

«Автоспорт» — окремий від Nika-Core самодостатній Windows-продукт з агентами. Він має розроблятися, тестуватися, пакуватися й запускатися незалежно від готовності Nika-Core. Перший vertical slice — настільний теніс, але canonical domain model має залишатися придатною для інших видів спорту, provider-ів, market types і settlement semantics.

V1 уже є реальним Windows-продуктом, а не CLI/demo: historical replay без future leakage, high-speed Market Mirror, Virtual Bank, singles/parlays, deterministic settlement, incremental Portfolio/Exposure Engine, agent orchestration, evaluation/learning loop та keyboard/NVDA-oriented UI.

Автоспорт **не є програмою «вгадай переможця»** і не є permanently paper-only продуктом. Зріла економічна мета — `maximize long-run bankroll growth subject to hard risk/execution constraints` через кілька рівноправних strategy families:

- predictive probability edge;
- live odds/state movement, lead/lag і stale actionable quote detection;
- cross-provider / cross-market discrepancies;
- arbitrage;
- dutching / full-outcome coverage;
- hedge / rebalance;
- багато singles/parlays/combinations, керованих як один economic portfolio;
- hybrid strategies.

Forecasting є strategy-class dependent, а не глобально обов’язковим. Predictive/hybrid intents використовують причинно коректний probability forecast. Pure arbitrage/dutching/hedging можуть бути валідними без directional winner forecast, якщо їхній proof спирається на causal executable quotes і complete terminal-state economics.

V1 навмисно залишається non-money-moving paper/replay/live-observation proof release. Поточна truth state: `REAL_MONEY_EXECUTION=false`. Це не постійна заборона продукту: після exact V1, bug bash і professional paper/live qualification той самий продукт розвивається через #355 live portfolio intelligence, bookmaker read-only capability, #353 supervised execution, real ledger/reconciliation і лише потім bounded autonomy.

Canonical long-horizon controls:
- Issue #1 — global product/control truth;
- Issue #198 — continuous roadmap;
- Issue #213 — mathematical intelligence;
- Issue #355 — live market / arbitrage / dutching / outcome-independent portfolio program;
- Issue #356 — generic opportunity decision contract, forecast optional by strategy class;
- Issue #353 — bookmaker/account real execution program.

## 2. Незалежність від Nika-Core

Повний fork Nika-Core не є базовою стратегією. Використовується Selective Reuse: стабільні нейтральні компоненти Nika можуть бути перенесені або адаптовані з точним source SHA, якщо це реально пришвидшує продукт. Перші кандидати на аудит: model gateway, multi-agent primitives, durable memory/state, interaction/browser adapters, task/runtime contracts.

Автоспорт має власні: repository, release cycle, Windows package, database, tests, CI, roadmap і coordination issue.

## 3. Product-wide + V1 одночасно

Розробка йде двома паралельними контурами:

- `PRODUCT-WIDE` — довгострокові contracts, event/data model, agents, storage, replay, portfolio math, live-market intelligence, observability, provider interfaces, Windows/accessibility і майбутні bookmaker/execution interfaces.
- `V1-CRITICAL-PATH` — найкоротший шлях до першого готового Windows proof release без money-moving execution.

Після V1 існують окремі product tracks, але вони не є окремими продуктами:

- `POST-V1 LIVE` — professional paper/live-observation qualification -> live price movement -> arbitrage/dutching/hedging -> whole-portfolio min-P&L;
- `POST-V1 EXECUTION` — bookmaker capability/account read -> supervised execution -> real ledger/reconciliation -> bounded autonomy.

Кожен task/PR має один primary lane tag: `PRODUCT-FOUNDATION`, `V1-CRITICAL`, `V1-QA`, `DATA`, `REPLAY`, `PORTFOLIO`, `AGENTS`, `LEARNING`, `PROVIDER`, `LIVE`, `ARBITRAGE`, `PERFORMANCE`, `WINDOWS`, `ACCESSIBILITY`, `RELEASE`, `BOOKMAKER`, `EXECUTION`, `DOCS`.

## 4. Базова архітектура

Event-driven pipeline:

`Provider Adapter → Normalizer → Canonical Market Event Bus → Market State Store/History → Opportunity/Strategy Layer → Incremental Portfolio Engine → Paper/Execution Plan → Settlement/Reconciliation → Evaluation/Learning`

LLM/AI не знаходиться у subsecond hot path. Ingestion, normalization, storage, current-state update, portfolio invalidation, money arithmetic, quote freshness і irreversible execution authority працюють звичайним deterministic code.

## 5. Canonical Market Model

Канонічні сутності: Sport, Competition, Participant, Match/Event, Market, Selection, Quote, ScoreState, MarketStatus, Result, SettlementRule.

V1 обов’язково підтримує winner/moneyline, totals, handicaps/fora; schema не повинна блокувати sets/games та інші market types.

Кожен Quote містить event_id, market_id, selection_id, decimal_odds, status, source_timestamp, observed_timestamp, ingest_timestamp, source_id, sequence/version.

## 6. High-Speed Market Mirror

Market Mirror тримає current state десятків і пізніше сотень матчів та append-only history усіх змін. Зберігаються odds, market status, score state, suspension/reopen, selection changes і provider sequence.

Hot path: deterministic parser → normalized event → in-memory state → append-only/time-series storage.

Якщо provider не дає точного source timestamp, quality of time позначається явно; система не вигадує точність. Stale, suspended або provenance-ambiguous quote не повинен автоматично ставати executable evidence.

Live observation і replay використовують один canonical market path. Mature live loop визначений #355:

`market/state update -> identity/provenance/freshness -> incremental analysis -> candidate discovery -> whole-portfolio delta -> min-P&L/risk -> execution feasibility -> optional supervised/bounded action -> acknowledgement/reconciliation -> repeat`.

## 7. Storage

V1: SQLite або DuckDB для metadata/transactions; Parquet або компактний append-only format для великих потоків quotes; in-memory cache для current state.

Invariants: idempotent writes, append-only normalized history, deterministic ordering, schema versioning, dataset checksums, crash-safe transactions, restart recovery без втрати paper portfolio.

## 8. Replay Engine

Історичні матчі відтворюються так, ніби відбуваються зараз. Strategy runtime бачить лише events з timestamp ≤ simulation_clock. Future quotes/results фізично ізольовані від Strategy layer.

Режими: 1×, 10×, 100×, event-driven jump-to-next-change, deterministic step-by-step.

Live observation і historical replay мають один `MarketEventStream` interface, щоб strategy code не залежав від джерела.

Кожен run має immutable run_id, dataset hash, strategy/model version, random seed, config snapshot, virtual bankroll і decision log.

## 9. Paper Book / Virtual Bank

Paper Book моделює bookmaker environment без реального грошового виконання. Кожний virtual ticket зберігає stake, type, legs, locked odds, potential payout, timestamps, strategy reason, risk state та settlement result.

Підтримуються singles і parlays/accumulators із configurable number of legs. Великі комбінації не перебираються brute-force; candidate generation використовує pruning.

Settlement Engine окремий від Strategy Agent і бачить final results тільки після завершення event/replay.

## 10. Portfolio / Exposure Engine

Центральне математичне ядро будує dependency graph між outcomes, legs, tickets, scenarios та aggregate P&L.

Система має в будь-який момент давати committed stake, current exposure, remaining live legs, best-case, worst-case, expected-case та sensitivity до outcome.

При зміні Quote/Score перераховується лише affected subgraph: dirty selections → affected candidates/tickets → affected portfolio/scenario nodes.

Для малого state space — exact enumeration. Для великого — factorization, dynamic programming, branch-and-bound, dominance pruning, scenario compression; Monte Carlo/importance sampling лише як явно позначений approximation mode.

Portfolio Coverage може заявити non-negative modeled worst-case лише після solver proof для чітко визначеного scenario space.

Сильніший truth label `OUTCOME_INDEPENDENT_POSITIVE` дозволений лише за #355 contract: complete relevant terminal outcome space, every terminal state evaluated, exact money semantics, compatible settlement rules, actionable fresh quotes, permitted stake vector, applicable fees/commission/tax, represented partial acceptance/rejection, feasible execution sequencing і `minimum terminal net P&L > 0`. Неповний або approximate proof не можна підвищувати до guaranteed/sure profit.

## 11. Probability / Forecasting та strategy classes

Детерміновані calculators відповідають за implied probabilities, overround normalization, stake/payout, EV, variance, drawdown, exposure, correlations, calibration metrics і scenario P&L.

AI/ML шар формує probability estimates, features, research hypotheses та explanations, але не замінює arithmetic engine.

Потрібна baseline-модель без LLM для чесного порівняння advanced predictive strategies.

Поточний V1 `ResearchDecisionPipeline` залишається forecast-bound для predictive paper path. Це не означає, що forecast є mandatory для всіх майбутніх opportunity classes. Issue #356 задає bounded successor contract: `ForecastRecord` required для predictive/hybrid probability-edge intents і optional/absent для pure price-structure strategies, якщо strategy-specific validator доводить causal quote/terminal-state economics без fabricated forecast.

## 12. Агентна система

- Coordinator Agent — orchestration і latency budgets.
- Research Agent — participant history/form/ranking/matchup evidence.
- Market Analyst Agent — structure and dynamics of normalized markets.
- Strategy Agent — paper decisions/candidate positions без future results.
- Live Opportunity Agent — discovery/classification of live price, arbitrage, dutching and hedge candidates outside deterministic arithmetic authority.
- Portfolio Agent — працює через deterministic Portfolio Engine.
- Risk Agent — paper stake/drawdown/concentration/correlation limits.
- Critic Agent — leakage/overfitting/weak-assumption review.
- Settlement Agent — deterministic post-event settlement.
- Learning/Evaluation Agent — post-settlement analysis і strategy revision proposals.
- Data Quality Agent — gaps, duplicates, timestamp/order anomalies, identity mismatches.

Агенти можуть створювати підзадачі, але hot-path ingestion/storage/math залишається ordinary code.

## 13. Learning loop

Кожне decision фіксується ДО результату з доступним context, probabilities when strategy-relevant, model/strategy/config version, features, odds і portfolio state. Після settlement додається result fact.

Evaluation використовує walk-forward/out-of-sample windows і immutable test periods. Метрики: bankroll curve, realized net P&L, ROI, max drawdown, risk of ruin, volatility, calibration/Brier/log loss для predictive strategies, P&L attribution, performance by market type/live-state regime, min-P&L capture where targeted, execution slippage/freshness buckets, hedge cost, arbitrage detected-vs-executable capture, turnover і stress scenarios.

Окремо показувати результат без найбільших випадкових wins. Жоден learning step не використовує інформацію, недоступну на decision timestamp.

## 14. Windows / Accessibility

V1 має packaged runnable Windows application. UI keyboard-first, main workflows без миші, коректні UIA names/roles/states. Усі графіки мають текстовий/табличний equivalent.

Основні екрани V1: Dashboard; Live/Replay Matches; Match Detail; Market History; Virtual Bank; Open/Settled Tickets; Portfolio Exposure; Agent Activity; Strategy Runs; Evaluation; Settings/Data Sources.

Physical Windows + NVDA acceptance — окремий gate; automated UIA не можна називати NVDA verification.

## 15. Technology direction V1

Control/agent/domain: Python 3.12+.

Data/numerical: NumPy/Polars + deterministic custom modules. Performance-critical solver після profiling може бути винесений у Rust/PyO3, але Rust не блокує старт.

Storage: SQLite/DuckDB + Parquet/append-only event files.

Async ingestion: asyncio, bounded queues, backpressure.

UI: desktop framework із перевіреним Windows UIA; framework може змінюватися, якщо цього вимагатимуть accessibility gates.

## 16. Performance targets

Engineering targets V1, що мають бути виміряні на цільовому Windows laptop:

- normalizer + local state update p95 < 100 ms, excluding network/provider delay;
- ordinary affected-subgraph portfolio recompute p95 < 100 ms;
- no unbounded queues;
- accelerated replay ≥ 100× real-time for a typical recording або максимально швидкий event-driven mode з опублікованим throughput.

Targets не є claims; release notes показують фактичні benchmarks.

## 17. Версії / milestones

Версії нижче — proof/research milestones усередині одного безперервного продукту; вони не встановлюють permanent paper-only ceiling і не скасовують post-V1 #355/#353 progression.

### V1 — Windows Paper/Replay/Live-Observation Proof Release

Реальний Windows release: table tennis vertical slice, historical replay without leakage, live observation adapter where available, Market Mirror, winner/totals/handicaps, Virtual Bank, singles/parlays, settlement, incremental Portfolio Engine, baseline model, agents, accessible UI, evaluation, import/export replay datasets, restart/recovery.

### V1.1 — Reliability and Performance

Endurance, provider resync, dataset validation, large portfolios, profiling, optional Rust accelerators, stronger UIA/NVDA acceptance.

### V2 — Multi-Sport / Multi-Provider Portfolio Lab

Інші sports, multiple providers, cross-provider entity resolution/history, advanced markets, correlation-aware models, larger parlays, stronger pruning/solvers, distributed collectors, strategy arena, scheduled replay campaigns.

### V3 — Learning Laboratory

Model/strategy registry, feature pipelines, walk-forward campaigns, champion/challenger, agent tournaments, calibration improvement, distributed replay workers, large corpora, reproducible training/evaluation artifacts.

### V4 — Scaled Autonomous Research Platform

Large-scale multi-machine workers, plugin SDK, provider sandbox, advanced scenario graph, richer memory, formal portfolio proofs where possible, enterprise observability, backup/restore, unattended paper research.

### Post-V1 mature-product progression

`exact V1 -> reliability/bug bash -> professional paper/live-observation qualification -> #355 live portfolio intelligence -> bookmaker read-only -> #353 supervised execution -> real execution ledger/reconciliation -> bounded autonomous execution under explicit user limits and emergency STOP`.

## 18. V1 Critical Path

Repository/bootstrap → domain schemas → append-only event log/current state → replay clock/leakage firewall → Paper Book/Virtual Bank → settlement → portfolio dependency graph → incremental calculator → table-tennis fixture dataset → baseline strategy → agent contracts/orchestrator → evaluation → Windows UI → packaging → restart/recovery → accessibility → endurance/performance.

Паралельна робота дозволена, але integration постійно має тримати runnable vertical slice.

## 19. Definition of Done V1

V1 DONE лише якщо одночасно є: runnable Windows artifact; keyboard-first UI; real agent orchestration; historical replay without future leakage; Market Mirror; Virtual Bank; singles/parlays; deterministic settlement; incremental Portfolio Engine; Research/Strategy/Risk/Critic/Data Quality/Learning roles; durable restart/recovery; reproducible run identities; evaluation metrics; exact-head green packaged tests; separate human NVDA acceptance record.

V1 DONE не означає `REAL_MONEY_EXECUTION=true`; money-moving authority має окремі post-V1 evidence/safety/provider/legal/reconciliation gates.

## 20. GitHub coordination

Канонічний control issue: `#1 AUTOSPORT — GLOBAL PRODUCT COMPLETION / V1 WINDOWS PAPER LAB`.

Live GitHub state має пріоритет над старими reports. Один semantic slice — один owner. Existing canonical lineage > duplicate implementation. Finish/converge useful work before distant feature work.

## 21. Перші задачі

1. Провести Nika-Core selective reuse audit з COPY / ADAPT / DO-NOT-COPY і source SHAs.
2. Закріпити canonical domain schemas і `MarketEventStream`.
3. Побудувати deterministic historical table-tennis fixture.
4. Реалізувати Replay + future-leakage firewall.
5. Реалізувати Virtual Bank / Paper Book / Settlement.
6. Реалізувати incremental Portfolio Engine.
7. Підключити baseline strategy та agent contracts.
8. Зібрати Windows shell.
9. Довести end-to-end replay до packaged Windows executable.

## 22. Головне правило

Автоспорт — не чат-бот, який «думає про ставки». Це високошвидкісна event-driven математична та агентна система, де AI формує гіпотези/strategy proposals, а дані, час, money arithmetic, portfolio dependencies, settlement, replay causality, quote freshness, execution feasibility та audit забезпечуються детермінованим ядром.

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`REAL_MONEY_EXECUTION=false`  
`V1_READY=false`
