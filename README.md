# Автоспорт / Autosport

**Professional agentic Windows sports-betting analysis, live-market intelligence, paper-proof and controlled execution platform.**

Автоспорт — окремий від Nika-Core продукт. Він розробляється, тестується, пакується та запускається незалежно від готовності Nika-Core.

## Product intent

Autosport **не є permanently paper-only продуктом** і **не є програмою “вгадай переможця”**.

Його зріла продуктова мета — максимізувати довгострокове зростання банку в межах жорстких risk/execution limits, використовуючи кілька незалежних джерел edge:

- predictive probability edge;
- live odds/state movement;
- lead/lag і stale actionable prices;
- cross-provider / cross-market discrepancies;
- arbitrage;
- dutching / full-outcome coverage;
- hedge / rebalance;
- багато singles/parlays/combinations, керованих як один портфель.

Forecasting — лише один із можливих input. Для чистого arbitrage/dutching/hedging directional forecast може бути непотрібним.

Зрілий цикл продукту:

`lawful data -> pre-match/live analysis -> optional forecast / market-state intelligence -> opportunity classification -> bankroll/portfolio/min-P&L risk -> multiple positions -> paper/live-observation proof -> bookmaker capability/account read -> supervised real execution -> real execution ledger/reconciliation -> bounded autonomous execution -> causal learning/adaptation`.

`REAL_MONEY_EXECUTION=false` означає лише, що поточна реалізація ще не має кваліфікованого real-money execution path. Це не постійна заборона продукту.

### Outcome-independent profit rule

Autosport може назвати портфель `OUTCOME_INDEPENDENT_POSITIVE` лише коли доведено повний релевантний terminal-state space і exact executable plan має `minimum terminal net P&L > 0` після settlement rules, stake granularity, provider/account limits, quote freshness/slippage, applicable fees/commission/tax, partial acceptance та execution sequencing.

Якщо доказ неповний, продукт зобов’язаний показати слабший truth label: theoretical arbitrage, execution risk, partial coverage, hedged-but-not-guaranteed або risked portfolio.

## V1 executable path

V1 навмисно є **non-money-moving proof release**. Він повинен довести причинність, точну paper-економіку, whole-portfolio risk, exact-vs-approximate scenario truth, restart/recovery та Windows/NVDA usability без ризику реальних коштів.

Перший vertical slice — настільний теніс, але canonical domain має залишатися sport-generic. V1 — реальний Windows-продукт з агентами, high-speed Market Mirror, sealed historical replay без future leakage, Virtual Bank, singles/parlays, deterministic settlement, incremental Portfolio/Exposure Engine, evaluation/learning loop та keyboard/NVDA-oriented UI.

Live observation використовує той самий canonical market path, що й replay. Швидкий ingestion/storage/portfolio math не залежить від LLM. AI працює над нормалізованими структурами й не замінює deterministic calculations.

У V1 реальні ставки **вимкнені**. Реальний bookmaker/account execution активується лише після окремих post-V1 доказів якості, risk control, прав/умов інтеграції та execution safety.

Пріоритет інтеграцій: official API -> sanctioned integration -> permitted browser automation. Майбутній execution layer повинен підтримувати account/balance readback, event/market/selection verification, bet-slip preparation, stake-vector entry, odds/slippage recheck, bookmaker acknowledgement, external bet IDs, open/settled position readback, reconciliation, partial multi-leg recovery та duplicate-bet prevention.

### Run

```powershell
python -m pip install -e .
python -m autosport demo
python -m autosport replay examples/table_tennis_replay.jsonl
python -m autosport dataset examples/tt_demo --workspace .autosport-workspace
python -m autosport gui
```

Tests:

```powershell
python -m unittest discover -s tests -v
```

Windows candidate:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
```

## Development model

- `PRODUCT-WIDE` — canonical contracts, sport/market/event model, storage, replay, portfolio math, agents, learning/evaluation, observability, Windows/accessibility, provider interfaces and future live/bookmaker/execution interfaces.
- `V1-CRITICAL-PATH` — shortest path to the first runnable Windows proof release without money-moving execution.
- `POST-V1 LIVE` — professional paper/live-observation qualification -> live price movement -> arbitrage/dutching/hedging -> whole-portfolio min-P&L.
- `POST-V1 EXECUTION` — bookmaker capability/account read -> supervised execution -> real ledger/reconciliation -> bounded autonomy.

## Work labels / lane tags

`PRODUCT-FOUNDATION`, `V1-CRITICAL`, `V1-QA`, `DATA`, `REPLAY`, `PORTFOLIO`, `AGENTS`, `LEARNING`, `PROVIDER`, `LIVE`, `ARBITRAGE`, `PERFORMANCE`, `WINDOWS`, `ACCESSIBILITY`, `RELEASE`, `BOOKMAKER`, `EXECUTION`, `DOCS`.

## Canonical control

- Repository: `Oleksii-debug/Autosport`
- Global coordination / product truth: GitHub Issue #1
- Live market / arbitrage / dutching / outcome-independent portfolio program: GitHub Issue #355
- Generic opportunity decision contract: GitHub Issue #356
- Long-horizon bookmaker execution program: GitHub Issue #353
- Mathematical intelligence: GitHub Issue #213
- Product roadmap: GitHub Issue #198
- Human-readable master specification: Google Drive folder `Автоспорт`, document `АВТОСПОРТ — MASTER TECHNICAL PROJECT`

## Product progression

- **V1 — Windows Paper/Replay/Live-Observation Proof Release**
- **V1.0.x — Reliability / Bug Bash**
- **Professional Paper + Live Qualification — bankroll/risk/live-opportunity proof**
- **Live Portfolio Intelligence — arbitrage/dutching/hedging/min-P&L**
- **Bookmaker Read-Only — capability, account/balance, limits, open/settled positions**
- **Supervised Execution — prepared single/multi-action plan + human confirmation**
- **Real Execution Ledger / Reconciliation — including partial multi-leg safety**
- **Bounded Autonomous Execution — only inside explicit user limits and kill-switch policy**
- **Continuous Mathematical Intelligence / Multi-Sport / Multi-Provider expansion**

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`REAL_MONEY_EXECUTION=false`  
`V1_READY=false`
