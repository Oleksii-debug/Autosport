# Автоспорт / Autosport

**Professional agentic Windows sports-betting analysis, live-market intelligence, paper-proof and controlled execution platform.**

Автоспорт — окремий від Nika-Core продукт. Він розробляється, тестується, пакується та запускається незалежно від готовності Nika-Core.

Autosport — **один продукт**, а не окремі продукти V1/V2/V3. Проміжні proof/release етапи існують лише як контрольовані кроки до повністю завершеного Autosport. Єдина ціль планування й розробки — `TIME_TO_WHOLE_FINISHED_PRODUCT`.

## Product intent

Autosport **не є permanently paper-only продуктом** і **не є програмою «вгадай переможця»**. Його зріла економічна мета — максимізувати довгострокове зростання банку в межах жорстких risk/execution limits, використовуючи кілька strategy families:

- predictive probability edge;
- live odds/state movement, lead/lag і stale actionable quotes;
- cross-provider / cross-market discrepancies;
- arbitrage і dutching / full-outcome coverage;
- hedge / rebalance;
- багато singles/parlays/combinations, керованих як один портфель.

Forecasting є strategy-class dependent, а не глобально обов’язковим. Для чистого arbitrage/dutching/hedging directional forecast може бути відсутнім, якщо рішення спирається на causal executable quote structure та повну terminal-state economics.

Зрілий цикл продукту:

`lawful data -> pre-match/live analysis -> optional forecast / market-state intelligence -> opportunity classification -> bankroll/portfolio/min-P&L risk -> multiple positions -> paper/live-observation proof -> bookmaker capability/account read -> supervised real execution -> real execution ledger/reconciliation -> bounded autonomous execution -> causal learning/adaptation`.

`REAL_MONEY_EXECUTION=false` означає лише, що поточна реалізація ще не має кваліфікованого real-money execution path. Це не постійна заборона продукту.

### Outcome-independent profit rule

Autosport може використовувати truth label `OUTCOME_INDEPENDENT_POSITIVE` лише коли доведено повний релевантний terminal outcome space і точний executable position/stake plan має `minimum terminal net P&L > 0` після settlement rules, stake granularity, provider/account limits, quote freshness/slippage, applicable fees/commission/tax, partial acceptance та execution sequencing assumptions.

Якщо доказ неповний або sampled/approximate, потрібен слабший truth label, наприклад `THEORETICAL_ARBITRAGE_ONLY`, `EXECUTION_RISK_PRESENT`, `PARTIAL_COVERAGE`, `HEDGED_BUT_NOT_GUARANTEED` або `RISKED_PORTFOLIO`. Детальний live/outcome-independent contract — GitHub Issue #355; generic strategy-class decision contract — Issue #356.

## Current executable path

Поточний runnable шлях навмисно починається з **non-money-moving proof**: replay, Virtual Bank, paper betting і live observation дозволяють без ризику реальних коштів довести causal correctness, exact money/portfolio calculations, exact-vs-approximate scenario truth, stake sizing, risk control, learning/evaluation, restart/recovery та Windows/NVDA usability.

Перший vertical slice — настільний теніс, але canonical domain має залишатися sport-generic. Поточний Windows path включає агентів, high-speed Market Mirror, sealed historical replay без future leakage, Virtual Bank, singles/parlays, deterministic settlement, incremental Portfolio/Exposure Engine, evaluation/learning loop та keyboard/NVDA-oriented UI.

Live observation використовує той самий canonical market path, що й replay. Швидкий ingestion/storage/portfolio math не залежить від LLM. AI працює над нормалізованими структурами й не замінює deterministic calculations.

На поточному етапі реальні ставки **вимкнені**. Реальний bookmaker/account execution активується лише після окремих доказів якості, risk control, прав/умов інтеграції та execution safety. Пріоритет інтеграцій: official API -> sanctioned integration -> permitted browser automation. Execution layer повинен підтримувати account/balance readback, event/market/selection verification, bet-slip preparation, stake-vector entry, odds/slippage recheck, bookmaker acknowledgement, external bet IDs, open/settled position readback, reconciliation, partial multi-leg recovery та duplicate-bet prevention.

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

Усі lane-и — частини одного Autosport і не є окремими продуктовими версіями.

- `PRODUCT-WIDE` — canonical contracts, sport/market/event model, storage, replay, portfolio math, agents, learning/evaluation, observability, Windows/accessibility, provider interfaces and live/bookmaker/execution interfaces.
- `RELEASE-CONVERGENCE` — shortest safe path from current main to a runnable, evidence-backed Windows candidate without misrepresenting whole-product completeness.
- `LIVE-INTELLIGENCE` — professional paper/live-observation qualification -> live price movement -> arbitrage/dutching/hedging -> whole-portfolio min-P&L.
- `EXECUTION-REALITY` — bookmaker capability/account read -> supervised execution -> real ledger/reconciliation -> bounded autonomy.
- `LEARNING-SCIENCE` — causal evidence -> evaluation -> champion/challenger -> drift -> safe promotion/rollback -> continual learning.

## Work labels / lane tags

`PRODUCT-FOUNDATION`, `RELEASE-CRITICAL`, `QA`, `DATA`, `REPLAY`, `PORTFOLIO`, `AGENTS`, `LEARNING`, `PROVIDER`, `LIVE`, `ARBITRAGE`, `PERFORMANCE`, `WINDOWS`, `ACCESSIBILITY`, `RELEASE`, `BOOKMAKER`, `EXECUTION`, `DOCS`.

## Canonical control

- Repository: `Oleksii-debug/Autosport`
- Whole-product control: GitHub Issue #1
- Continuous roadmap: GitHub Issue #198
- Swarm operating constitution: GitHub Issue #362
- Scientific truth: GitHub Issue #367
- Live dispatch / semantic ownership: GitHub Issue #368
- Unlimited work-packet bank: GitHub Issue #763
- Mathematical intelligence: GitHub Issue #213
- Live market / arbitrage / dutching / outcome-independent portfolio program: GitHub Issue #355
- Generic opportunity decision contract: GitHub Issue #356
- Long-horizon bookmaker execution program: GitHub Issue #353
- Human-readable master specification: Google Drive folder `Автоспорт`, document `АВТОСПОРТ — MASTER TECHNICAL PROJECT`

## Capability progression inside one product

These are dependency steps, not separate product versions:

- **Windows Paper/Replay/Live-Observation Proof** — causal product-path proof without money-moving execution.
- **Reliability / Bug Bash / Endurance** — restart, recovery, long-run integrity and operator evidence.
- **Professional Paper + Live Qualification** — bankroll/risk/live-opportunity proof.
- **Live Portfolio Intelligence** — arbitrage/dutching/hedging/min-P&L.
- **Bookmaker Read-Only** — capability, account/balance, limits, open/settled positions.
- **Supervised Execution** — prepared single/multi-action plan + human confirmation.
- **Real Execution Ledger / Reconciliation** — including partial multi-leg safety.
- **Bounded Autonomous Execution** — only inside explicit user limits and kill-switch policy.
- **Continuous Mathematical Intelligence / Multi-Sport / Multi-Provider expansion** — scientific and product maturation toward whole-product completion.

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`REAL_MONEY_EXECUTION=false`  
`WHOLE_PRODUCT_COMPLETE=false`
