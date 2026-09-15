# АВТОСПОРТ — MASTER TECHNICAL PROJECT

## 1. Кінцева продуктова мета

«Автоспорт» — окремий від Nika-Core самодостатній Windows-продукт і професійна багатоагентна система спортивного беттінгу. Він розробляється, тестується, пакується й запускається незалежно від готовності Nika-Core.

Перший vertical slice — настільний теніс, але canonical domain model має залишатися sport-generic і придатною для інших видів спорту, provider-ів, market types та settlement semantics.

Autosport **не є програмою “вгадай переможця”**. Його зріла економічна мета — довгострокове зростання банку в межах жорстких user-configured risk/execution limits.

Forecasting — лише одне джерело edge. Рівноправні strategy families:

- predictive probability edge;
- live odds/state movement;
- lead/lag та stale actionable quote detection;
- cross-provider/cross-market discrepancy;
- arbitrage;
- dutching/full-outcome coverage;
- hedge/rebalance;
- багато singles/parlays/combinations, керованих як один portfolio;
- hybrid strategies.

Для price-structure strategies directional winner forecast може бути непотрібним.

Canonical long-horizon controls:
- Issue #1 — global product/control truth;
- Issue #355 — live market / arbitrage / dutching / outcome-independent portfolio program;
- Issue #356 — generic opportunity decision contract, forecast optional by strategy class;
- Issue #353 — bookmaker/account real execution program;
- Issue #213 — mathematical intelligence;
- Issue #198 — continuous roadmap.

`REAL_MONEY_EXECUTION=false` — поточна truth state, а не постійна межа продукту.

## 2. Один продукт, поетапне підвищення довіри

V1 використовує paper/replay/live-observation не як кінцеву бізнес-мету, а як proving ground перед money-moving authority.

Product progression:

1. exact V1 Windows proof release;
2. V1.0.x bug bash / correctness hardening;
3. professional paper + live-observation qualification;
4. #355 live portfolio / arbitrage / dutching / hedge intelligence;
5. bookmaker/account read-only capability;
6. supervised execution;
7. separate real execution ledger/reconciliation including partial multi-leg safety;
8. bounded autonomous execution under explicit external user limits and emergency STOP;
9. continuous causal learning/champion-challenger improvement.

V1 не має передчасно реалізовувати money-moving features, якщо це затримує release, але V1 contracts не повинні створювати тупикову архітектуру для майбутнього live/real execution.

## 3. Незалежність від Nika-Core

Повний fork Nika-Core не є базовою стратегією. Використовується Selective Reuse / REUSE → ADAPT → THIN CUSTOM: стабільні нейтральні компоненти можуть бути перенесені або адаптовані з exact source identity, якщо це зменшує ризик і пришвидшує Autosport.

Не переносити shared god-runtime або компоненти, які створюють залежність від незавершеної Nika.

Autosport має власні repository, release cycle, Windows package, canonical storage, tests, CI, roadmap та coordination control.

## 4. Product-wide + V1 одночасно

- `PRODUCT-WIDE` — sport/market/event contracts, storage, replay, portfolio math, agents, learning/evaluation, live-market intelligence, provider/bookmaker interfaces, observability, recovery, Windows/accessibility.
- `V1-CRITICAL-PATH` — найкоротший шлях до exact trusted Windows proof release без money-moving execution.

Primary lane tags можуть включати:
`PRODUCT-FOUNDATION`, `V1-CRITICAL`, `V1-QA`, `DATA`, `REPLAY`, `PORTFOLIO`, `AGENTS`, `LEARNING`, `PROVIDER`, `LIVE`, `ARBITRAGE`, `PERFORMANCE`, `WINDOWS`, `ACCESSIBILITY`, `RELEASE`, `BOOKMAKER`, `EXECUTION`, `DOCS`.

Live native/source ownership and F01 integration authority мають пріоритет над плановими документами. One semantic slice = one source owner.

## 5. Базова архітектура

Event-driven pipeline:

`Provider Adapter -> Normalizer -> Canonical Market Event Bus -> Market State Store/History -> Incremental Portfolio/Opportunity Engine -> Strategy/Agent Layer -> Paper/Execution Plan -> Settlement/Reconciliation -> Evaluation/Learning`

LLM/AI не знаходиться у subsecond hot path. Ingestion, normalization, storage, quote freshness/order, current-state update, portfolio invalidation, exact money math, min-P&L, risk limits та execution identity працюють детермінованим кодом.

## 6. Canonical Market Model

Canonical entities include:
`Sport`, `Competition`, `Participant`, `Match/Event`, `Market`, `Selection`, `Quote`, `ScoreState`, `MarketStatus`, `Result`, `SettlementRule`, `Provider/Bookmaker`, `Ticket/Position`, `Portfolio`, `Scenario`, `ExecutionPlan`, `ExecutionRecord`.

V1 мінімально підтримує winner/moneyline, totals, handicaps/fora; schema не повинна блокувати sets/games та інші sports/markets.

Кожен Quote має bind:
- sport/event/market/selection identity;
- provider/bookmaker identity;
- decimal odds/price;
- market status/suspension;
- source timestamp;
- receive/observed timestamp;
- ingest timestamp;
- sequence/version where available;
- provenance/quality/freshness state.

Stale/suspended/ambiguous quote не може вважатися executable.

## 7. High-Speed Market Mirror

Market Mirror тримає current state десятків/сотень матчів та append-only history змін.

Hot path:
`deterministic parser -> normalized event -> in-memory current state -> append-only canonical storage -> affected-subgraph invalidation`.

Зберігати odds, market status, score state, suspension/reopen, selection changes, provider sequence, source/receive timing і quality flags.

Live observation та historical replay використовують один canonical event contract, щоб strategy/opportunity code не залежав від джерела.

## 8. Canonical storage / data plane

V1 canonical mutable operational/transaction authority — **SQLite**.

Не вводити DuckDB/PostgreSQL/Parquet як другий canonical economic truth у V1. DuckDB/Polars/Parquet/PyArrow можуть з'явитися пізніше як derived read-only analytical/corpus acceleration після benchmark evidence.

Invariants:
- idempotent writes;
- append-only raw/normalized history where required;
- deterministic ordering;
- schema versioning;
- exact checksums/identity;
- crash-safe transaction boundaries;
- restart/recovery without economic duplication;
- no secret leakage into artifacts/logs.

## 9. Replay Engine / causal firewall

Historical replay відтворює market stream так, ніби подія відбувається зараз. Runtime бачить лише evidence з `available_at <= simulation_clock`.

Future quotes/results фізично ізольовані до causal reveal. Learning/evaluation не може заднім числом переписати earlier decision evidence.

Режими можуть включати 1x, accelerated 10x/100x, event-jump та deterministic step-by-step.

Кожен run binds immutable run identity, dataset/source identity, strategy/model/config identity, cutoffs, seed where relevant, starting bankroll та decision evidence.

## 10. PaperBook / Virtual Bank

PaperBook моделює bookmaker economics без реального money movement.

Кожний virtual ticket/position зберігає:
- stake;
- ticket/position type;
- legs;
- decision-time locked odds;
- potential payout;
- timestamps;
- strategy/opportunity identity;
- risk/portfolio context;
- settlement result.

Підтримуються singles і parlays/combinations із configurable legs. Candidate search використовує pruning/solver logic, а не безмежний brute force.

Settlement Engine окремий від Strategy/Opportunity decision path і бачить authoritative outcome лише після causal reveal.

## 11. Portfolio / Exposure / Opportunity Engine

Центральне економічне ядро моделює dependency graph між outcomes, selections, tickets/positions, parlays, providers і terminal scenarios.

Виходи:
- available/reserved bankroll;
- committed stake/capital at risk;
- current exposure;
- event/market/provider concentration;
- best-case terminal P&L;
- minimum/worst-case terminal P&L;
- expected P&L only when valid probability evidence exists;
- exact/approximate/completeness label;
- outcome coverage;
- drawdown/risk metrics;
- marginal effect of each candidate;
- hedge/rebalance/dutching alternatives.

На quote/score/state change перераховується лише affected dependency subgraph where correctness permits.

For small complete spaces use exact enumeration. For larger spaces use factorization, dynamic programming, branch-and-bound, constraint solving, scenario compression or sampling, but approximation must be explicit.

Sampling/Monte Carlo can estimate risk; it can never prove guaranteed positive minimum P&L over an incomplete terminal-state space.

## 12. Outcome-independent profit contract

Portfolio може бути labelled `OUTCOME_INDEPENDENT_POSITIVE` лише коли:

- complete relevant terminal outcome space is proven;
- every terminal state is evaluated;
- exact stake/payout semantics used;
- provider/bookmaker settlement rules are compatible;
- quotes are actionable/fresh;
- provider/account limits allow the stake vector;
- stake granularity is included;
- applicable fees/commission/tax are included;
- partial acceptance/rejection is represented;
- execution sequencing/atomicity assumptions are feasible;
- `minimum terminal net P&L > 0`.

Otherwise use weaker truth labels such as:
`THEORETICAL_ARBITRAGE_ONLY`, `EXECUTION_RISK_PRESENT`, `PARTIAL_COVERAGE`, `HEDGED_BUT_NOT_GUARANTEED`, `RISKED_PORTFOLIO`.

A simple inverse-odds test is only a fast candidate screen for suitable mutually exclusive outcomes, not final execution proof.

## 13. Probability / forecasting / mathematical intelligence

Deterministic calculators own odds conversion, overround/de-vig primitives, stake/payout, EV, variance, drawdown, exposure, scenario P&L and exact portfolio arithmetic.

Forecast models may estimate probabilities, features and uncertainty. They are required only for strategy classes that claim probability/predictive edge.

Mature generic opportunity contract (#356) supports at minimum:
- `PREDICTIVE_EDGE`;
- `LIVE_PRICE_MOVEMENT`;
- `ARBITRAGE`;
- `DUTCHING`;
- `HEDGE_REBALANCE`;
- `HYBRID`.

Pure price-structure/arbitrage/dutching/hedge decisions may not require a directional forecast; their proof comes from causal quote/state evidence + portfolio/settlement/stake-vector economics.

External numerical/scientific libraries are optional post-V1/measured-need engines behind Autosport-owned ports. They never become economic, causal, provenance or release authority.

## 14. Live-market intelligence

Live is a first-class mature-product earning lane.

Required live loop:

`market/state update -> identity/provenance/freshness/order -> incremental analysis -> opportunity candidates -> portfolio delta -> min-P&L/risk -> execution feasibility -> supervised/bounded execution -> acknowledgement/reconciliation -> repeat`.

Support future analysis of:
- quote velocity/acceleration/reversal;
- provider disagreement and lead/lag;
- stale quote detection;
- regime/volatility changes;
- short-horizon market-state transitions;
- cross-provider/cross-market actionable discrepancies.

No cached recommendation survives quote expiry automatically.

## 15. Агентна система

Possible typed roles:
- Coordinator Agent;
- Research/Data Quality Agent;
- Market Analyst Agent;
- Forecast/Predictive Agent;
- Live Opportunity Agent;
- Strategy/Opportunity Planner;
- Portfolio Agent;
- Risk/Critic Agent;
- Settlement Agent;
- Learning/Evaluation Agent.

Agents work through canonical stores/contracts and cannot bypass deterministic money/risk/execution authorities.

Risk limits are external/user-configured authority. No model/agent may silently enlarge stake, loss, turnover, exposure or automation limits.

## 16. Learning / champion-challenger loop

Every decision is frozen before outcome with exact available evidence, quote/state, portfolio state, strategy/model/config identity, cutoffs and action/rejection reason.

After authoritative reveal evaluate:
- realized net P&L;
- bankroll growth;
- EV capture where applicable;
- calibration for predictive models;
- max drawdown / risk of ruin;
- turnover;
- live quote age/freshness;
- execution slippage;
- rejected/partial execution rate;
- hedge cost;
- arbitrage detected-vs-captured;
- performance by sport/provider/market/live regime.

Promotion requires causal walk-forward/holdout evidence and must fail closed on material protective-metric deterioration.

## 17. Real bankroll / stake policy

A real account balance is not unrestricted execution authority.

Example: 10,000 UAH bank can still imply 50 UAH or zero for a position depending on risk, edge, open exposure and user limits.

For arbitrage/dutching/hedging evaluate the whole stake vector, not each leg independently.

Configurable future limits include:
- max stake per bet;
- max stake/capital at risk as bankroll fraction;
- max session/day loss;
- max turnover;
- max event/market/provider exposure;
- max concurrent positions;
- max parlay/combination exposure;
- max slippage;
- min quote freshness;
- per-agent/automation authority;
- emergency global STOP/kill switch.

## 18. Bookmaker execution / reconciliation

After paper/live qualification, #353 governs:

- Bookmaker Capability Registry;
- official API first, sanctioned integration second, permitted browser automation where applicable;
- account/balance/limits read-only;
- event/market/selection verification;
- bet-slip/action preparation;
- current odds/freshness/slippage recheck;
- stake/stake-vector entry;
- acknowledgement + external bet IDs;
- open/settled position readback;
- balance reconciliation;
- duplicate-bet prevention;
- partial multi-leg safety;
- bounded autonomy.

A P0 hazard is one accepted leg with later hedge/arbitrage legs rejected or repriced. After every acknowledgement recompute the actual portfolio, invalidate stale remaining plan actions, reconcile external IDs and continue only with a fresh plan within limits.

PaperBook is never reused as fake real-account truth. Real money requires a separate execution ledger.

## 19. Windows / Accessibility

Current V1 UI path remains **Tk + tk-uia** through physical acceptance. Do not rewrite to another UI stack merely from preference.

Packaged application must be keyboard-first. Machine UIA checks verify prerequisites, but only physical Windows 11 + NVDA evidence can set `HUMAN_TESTED` / `NVDA_VERIFIED`.

Critical future execution surfaces—recommendation, min-P&L/risk, approval, balance, acknowledgement, rejection, reconciliation and STOP—must have textual keyboard/screen-reader access.

## 20. Performance direction

Subsecond updates never depend on LLM. Collectors, normalizers, event bus, current state, quote freshness and affected-subgraph portfolio recomputation are local deterministic code.

V1 performance budgets remain engineering targets to measure honestly. Optimize from profiler/benchmark evidence; indexes/batching/SQLite and algorithmic pruning first, heavier vector/columnar/runtime engines later if justified.

## 21. Version / roadmap interpretation

Version numbers are milestones, not permanent scope walls.

### V1 — Windows Paper/Replay/Live-Observation Proof
Causal replay, Market Mirror, Virtual Bank, singles/parlays, settlement, portfolio truth, agents, recovery, accessible Windows package.

### V1.0.x — Reliability / Bug Bash
Crash/data-loss/economic/accessibility/recovery/security defects before feature breadth.

### Professional Paper + Live Qualification
Long-running lawful historical and forward/live observation proving bankroll/risk/live-opportunity behavior.

### Live Portfolio Intelligence (#355)
Arbitrage, dutching, hedge/rebalance, cross-provider/cross-market opportunity classification and exact min-P&L truth.

### Bookmaker Read-Only / Supervised / Real Execution (#353)
Controlled progression to real-account execution and reconciliation.

### Multi-Sport / Mathematical Intelligence
Expand sport/provider/model breadth without weakening canonical economic/causal/execution truth.

No V1–V4 wording may be interpreted as a permanent prohibition on the mature real-execution goal.

## 22. V1 Critical Path

Repository/bootstrap -> canonical domain schemas -> append-only event log/current state -> replay clock/leakage firewall -> PaperBook/Virtual Bank -> settlement -> portfolio dependency graph -> incremental calculator -> lawful fixture/market dataset -> baseline strategy -> agent contracts/orchestrator -> evaluation -> Windows UI -> packaging -> restart/recovery -> accessibility -> endurance/performance.

Keep runnable vertical slice. Do not accumulate duplicate framework implementations.

## 23. Definition of Done V1

V1 DONE only when one exact integrated packaged Windows candidate proves:
- keyboard-first UI;
- real agent orchestration;
- replay without future leakage;
- Market Mirror;
- Virtual Bank;
- singles/parlays;
- deterministic settlement;
- incremental Portfolio Engine;
- exact-vs-approximate/completeness truth;
- durable restart/recovery;
- reproducible identities/evidence;
- evaluation;
- exact-head required machine gates;
- separate physical NVDA acceptance.

Real-money execution remains disabled in V1, but that is a stage boundary, not mature product scope.

## 24. Головне правило

Autosport — не чат-бот і не “winner predictor”. Це high-speed event-driven mathematical, agentic and execution-oriented betting system. AI can research and propose; canonical data/time/money/portfolio/settlement/replay/execution/reconciliation truth remains deterministic and auditable.

`REAL_MONEY_EXECUTION=false`
`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`V1_READY=false`
