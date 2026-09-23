# АВТОСПОРТ — MASTER TECHNICAL PROJECT

> **BINDING OWNER CORRECTION:** у Autosport є **одна програма, одна кінцева ціль і одне визначення готовності** — повністю завершений кінцевий Autosport від А до Я. Будь-які старі назви `V1`, `V1.1`, `V2`, `V3`, `V4`, `post-V1`, `first version`, `proof release` тощо є лише історичними назвами старих етапів/артефактів і **не мають права розділяти продукт, задавати окремий відсоток готовності або бути точкою зупинки**.
>
> Канонічна додаткова фіксація: `docs/WHOLE_PRODUCT_COMPLETION_AUTHORITY.md`.

## 1. Єдина продуктова мета

«Автоспорт» — самодостатній Windows-продукт з агентами, який має бути доведений до **повністю завершеної кінцевої системи**, а не до окремої “першої версії”.

Кінцевий продукт — професійна багатоагентна система спортивного аналізу, live-market intelligence, причинно коректного навчання, керування банком/ризиком, побудови портфеля ставок і, після необхідних доказів безпеки/якості/правомірності, контрольованого виконання реальних ставок.

Autosport **не є програмою “вгадай переможця”**. Forecasting — лише один із можливих джерел edge. Кінцева економічна мета:

`maximize long-run bankroll growth subject to hard risk/execution constraints`

через рівноправні strategy families:

- predictive probability edge;
- live odds/state movement, lead/lag і stale actionable quote detection;
- cross-provider / cross-market discrepancies;
- arbitrage;
- dutching / full-outcome coverage;
- hedge / rebalance;
- singles/parlays/combinations як один economic portfolio;
- hybrid strategies.

Повний цикл кінцевого продукту:

`lawful data -> pre-match/live market state -> research/forecast/other opportunity intelligence -> multi-agent decision -> whole-portfolio economics/risk -> endogenous stake/stake-vector/WAIT/ZERO -> live opportunity search -> bookmaker capability/read -> supervised execution -> real execution ledger/reconciliation -> bounded autonomous execution under owner limits -> settlement -> evaluation -> continual causal learning/research -> accessible Windows product`

## 2. Єдине правило завершення

Немає окремого “V1 DONE”, “post-V1 DONE” або іншої версійної фінішної лінії.

**Autosport DONE** лише коли весь погоджений кінцевий продукт від А до Я реалізований, інтегрований, перевірений і придатний до реального використання відповідно до owner requirements.

Будь-який проміжний package, paper/replay доказ, Windows build, machine-accessibility pass, supervised execution milestone або інший gate — лише залежність/етап всередині єдиного продукту. Він ніколи не є окремим продуктом і не дає права зупиняти роботу над наступними потрібними capabilities.

## 3. Правило прогресу

Є лише **один відсоток готовності — готовність повного кінцевого Autosport**.

Заборонено:

- рахувати окремий відсоток “V1”;
- порівнювати “V1 readiness” із “whole product readiness”;
- повторювати старий відсоток через дні матеріальної роботи без нового capability audit;
- підміняти прогрес кількістю PR, commits, tests або worker launches;
- залишати відсоток незмінним лише тому, що старий audit колись його зафіксував.

Кожний новий звіт має оцінювати поточний live стан capability-by-capability і пояснювати, що реально змінилося з попереднього зрізу.

## 4. Принцип розробки

Робота може бути розбита на технічні етапи, залежності й safety gates, але **не на продуктові версії**.

Основна оптимізаційна ціль усіх workers/auditors:

`TIME_TO_WHOLE_FINISHED_PRODUCT`

Не оптимізувати окремо “time to first release”, PR count, issue count, test count або activity volume.

Після завершення одного capability worker переходить до наступного найважливішого незавершеного capability, якщо ownership/WIP/safety дозволяють.

## 5. Базова архітектура

Event-driven pipeline:

`Provider Adapter -> Normalizer -> Canonical Market Event Bus -> Market State Store/History -> Opportunity/Strategy Layer -> Incremental Portfolio Engine -> Paper/Execution Plan -> Settlement/Reconciliation -> Evaluation/Learning`

LLM/AI не знаходиться у subsecond hot path. Ingestion, normalization, storage, current-state update, portfolio invalidation, money arithmetic, quote freshness і irreversible execution authority працюють deterministic code.

## 6. Canonical Market Model

Канонічні сутності: Sport, Competition, Participant, Match/Event, Market, Selection, Quote, ScoreState, MarketStatus, Result, SettlementRule.

Canonical domain model має бути придатною для багатьох видів спорту, providers, market types і settlement semantics. Table tennis може бути першим практичним vertical slice, але не є продуктовою межею.

Кожен Quote містить стабільну event/market/selection/source identity, decimal odds, status, source/observed/ingest timestamps і sequence/version.

## 7. High-Speed Market Mirror

Market Mirror підтримує current state і append-only history змін: odds, market status, score state, suspension/reopen, selection changes, provider sequence і provenance.

Hot path:

`deterministic parser -> normalized event -> canonical durable history/current projection -> focused incremental invalidation`

Якщо provider не дає точного source timestamp, система явно фіксує невизначеність і не вигадує точність. Stale, suspended або provenance-ambiguous quote не стає executable evidence.

Live loop:

`market/state update -> identity/provenance/freshness -> incremental analysis -> candidate discovery -> whole-portfolio delta -> min-P&L/risk -> execution feasibility -> supervised/bounded action when authorized -> acknowledgement/reconciliation -> repeat`

## 8. Storage / restart / recovery

Canonical persistence має забезпечувати:

- idempotent writes;
- append-only normalized history;
- deterministic ordering;
- schema versioning;
- dataset/checkpoint hashes;
- crash-safe transactions;
- process restart/recovery;
- fail-closed corruption handling;
- single authority per durable truth domain.

SQLite/DuckDB/Parquet/append-only event formats можуть використовуватися там, де це виправдано архітектурою й performance evidence.

## 9. Replay / causal correctness

Історичні події відтворюються так, ніби відбуваються зараз. Strategy/runtime бачить лише дані, доступні на simulation/decision timestamp.

Future quotes/results/outcome-derived memory фізично або capability-bound ізольовані від decision path.

Потрібні deterministic replay, walk-forward, causal cutoffs, restart-safe run identity та immutable evidence.

## 10. Paper Book / Virtual Bank як доказовий режим

Paper Book моделює bookmaker environment без money-moving action. Він потрібний для безпечної перевірки settlement, bankroll, stake sizing, portfolio/risk і learning logic.

Paper/replay режим — **не окремий продукт і не кінцева версія**. Це один необхідний safety/evidence stage всередині повного Autosport.

Підтримуються singles, parlays/combinations, multiple simultaneous positions, deterministic settlement і exact money semantics.

## 11. Economic Goal / Risk / endogenous decisions

Кінцевий продукт має first-class `EconomicGoalContract`, окремий від executable `RiskPolicy`.

Користувач задає мету й максимальні межі authority/risk, а система сама визначає stake/stake-vector/hedge/rebalance/WAIT/ZERO в межах цих обмежень.

Agent/model не може самостійно розширювати owner authority, loss limits, exposure, automation level або execution scope. Автоматичне safety tightening дозволене й audit-able.

## 12. Portfolio / Exposure Engine

Центральне математичне ядро оцінює **весь open + proposed portfolio**, а не одну ставку ізольовано.

Воно має підтримувати:

- committed stake;
- current exposure;
- remaining live legs;
- best/worst/expected terminal P&L;
- dependency/correlation graph;
- affected-subgraph incremental recomputation;
- hedge/rebalance alternatives;
- exact-vs-approximate truth.

`OUTCOME_INDEPENDENT_POSITIVE` дозволений лише при complete terminal-state proof, exact money semantics, compatible settlement rules, actionable fresh quotes, applicable costs/limits, represented partial execution and feasible sequencing, де exact minimum terminal net P&L > 0.

## 13. Opportunity language

Єдина strategy-class-independent opportunity/portfolio decision boundary повинна підтримувати predictive edge, live movement, lead/lag, arbitrage, dutching, hedge/rebalance, parlay/hybrid та WAIT/ZERO.

Forecast є mandatory лише для strategy classes, де він справді потрібний. Pure price-structure opportunity може бути валідним без directional forecast, якщо causal executable quote/terminal-state evidence достатній.

## 14. Scientific memory / Strategy & Model Factory

Autosport має durable scientific memory для:

- hypotheses/research questions;
- experiments;
- dataset/feature snapshots;
- model/strategy versions;
- evaluation bundles;
- promotion/rejection/rollback decisions;
- negative/null/harmful results;
- reproducibility evidence.

Factory має забезпечити baseline models, causal walk-forward evaluation, champion/challenger, deterministic promotion rules, drift detection, rollback і fail-closed provenance.

Self-learning — не LLM reflection. Воно має бути причинно коректним:

`OBSERVE -> ACT -> OUTCOME/REWARD -> UPDATE -> RETEST`

Жодна learned policy не може самостійно збільшити financial authority.

## 15. Continuous research supervisor

Кінцевий продукт має підтримувати restart-safe довгоживучий research loop:

`hypothesis -> experiment -> causal evaluation -> evidence -> accept/reject -> next hypothesis`

Scheduler може бути trigger layer, але не другою truth authority. Кожна робота має durable run/checkpoint/idempotency identity.

## 16. Multi-agent system

Принаймні:

- Coordinator/Orchestrator;
- Research Agent;
- Market Analyst Agent;
- Strategy/Opportunity Agent;
- Portfolio Agent;
- Risk Agent;
- Critic/Red-team Agent;
- Settlement/Reconciliation Agent;
- Learning/Evaluation Agent;
- Data Quality Agent.

Agents можуть створювати підзадачі, але storage/math/money/execution truth залишається deterministic authority.

## 17. Bookmaker capability / account read

Кінцевий Autosport має canonical bookmaker capability profiles для supported providers/accounts:

- markets/sports;
- live/pre-match quote capability;
- account/balance read;
- limits;
- open/settled positions;
- bet-slip/place capability;
- authentication/integration mode;
- legal/terms/automation status where known.

Починати з read-only/account reconciliation capability там, де це технічно й правомірно; це лише dependency stage, не окрема версія продукту.

## 18. Real execution

Money-moving execution є частиною кінцевої програми, але активується лише після необхідних safety/evidence gates.

Потрібні:

- ExecutionPlan;
- Attempt;
- Acknowledgement;
- external receipt identity;
- RealExecutionLedger;
- Reconciliation;
- ExecutionSaga;
- timeout/UNKNOWN handling;
- duplicate-placement prevention;
- partial multi-leg recovery/rebalance;
- user-configured automation levels;
- emergency STOP.

Supervised execution перед bounded autonomy — safety ordering всередині одного продукту.

## 19. Learning / evaluation metrics

Кожне decision фіксується ДО outcome reveal з model/strategy/config, features, odds, quote provenance, portfolio state і decision timestamp.

Оцінювати не лише win-rate, а bankroll growth, realized P&L, drawdown, risk-of-ruin, calibration/EV where relevant, turnover, concentration, slippage, rejection/partial rate, hedge cost, arbitrage detected-vs-captured, stability and reproducibility.

Promotion заборонений при future leakage або material protective-metric degradation.

## 20. Windows / Accessibility / Ukrainian-first

Windows — first-class user surface кінцевого продукту.

Обов’язково:

- packaged runnable application;
- keyboard-first navigation;
- no mouse-only critical path;
- stable UIA names/roles/values/readonly/focus semantics;
- Ukrainian-first user-facing catalog;
- textual/tabular parity for visual information;
- accessible error/status/reconciliation/execution confirmation;
- restart/recovery;
- physical Windows + NVDA acceptance on exact artifact.

Machine UIA pass не можна називати human/NVDA verification.

## 21. Performance

Performance targets мають бути виміряні на реальному target environment і не можуть підміняти correctness.

Hot paths повинні бути bounded; queues — bounded; incremental affected-subgraph updates мають перевагу над full recomputation, якщо correctness доведена.

Targets є engineering goals, а не claims без вимірювання.

## 22. Multi-sport / multi-provider direction

Canonical domain має залишатися sport-generic. Розширення відбувається sport-by-sport/provider-by-provider з lawful data, verified semantics і без жорсткого hardcode під один вид спорту.

Не будувати десять providers/sports одночасно лише заради кількості; розширювати там, де це реально скорочує TIME_TO_WHOLE_FINISHED_PRODUCT.

## 23. Safety / authority invariants

Ніколи не жертвувати заради швидкості:

- no-future-leakage;
- exact money/Decimal semantics;
- provenance/evidence identity;
- lawful data/automation boundaries;
- settlement/recovery correctness;
- exact-vs-approximate labels;
- receipt reconciliation before retry;
- duplicate real-action prevention;
- owner risk/authority limits;
- physical NVDA distinction;
- fail-closed ambiguity around irreversible actions.

## 24. Coordination

Canonical control:

- Issue #1 — whole finished product control;
- Issue #198 — one whole-product roadmap;
- Issue #362 — binding swarm operating constitution;
- Issue #368 — live task dispatch/claim protocol;
- `docs/WHOLE_PRODUCT_COMPLETION_AUTHORITY.md` — binding owner correction against version-splitting.

Live GitHub state > old reports. Existing canonical lineage > duplicate implementation. One semantic slice = one source owner. Integration remains serialized where required.

Historical task IDs/semantic keys may still contain `V1` for identity continuity. **Це не дає їм права визначати продуктову ціль.**

## 25. Worker selection law

At every launch:

1. refresh live main/open PRs/ownership/control;
2. identify the highest-value unfinished capability for the final whole product;
3. respect dependencies/WIP/ownership and finish useful existing lineages before duplicates;
4. take the largest safe causally complete work unit;
5. implement/review/test/integrate or hand off with evidence;
6. refresh and continue to the next whole-product blocker;
7. if mutation is blocked, do useful read-only qualification/research instead of invented churn.

No worker may stop because a historical “V1” milestone is satisfied.

## 26. Current truth flags

Current implementation truth flags may exist for safety/verification bookkeeping, but none of them is the sole product-completion definition.

`REAL_MONEY_EXECUTION=false`  
`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`

Legacy `V1_READY` references may remain in historical code/issues for compatibility/audit identity, but **must not be interpreted as whole-product readiness or a separate finish line**.

## 27. Головне правило

Автоспорт — не чат-бот, який “думає про ставки”, і не набір окремих версій. Це **одна завершувана система**: високошвидкісне event-driven deterministic ядро + agents + scientific learning + portfolio/risk + live intelligence + bookmaker/execution/reconciliation + Windows/NVDA-accessible product.

**Єдина кінцева ціль: повністю завершений Autosport від А до Я.**
