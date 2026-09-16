# Автоспорт / Autosport

**Professional agentic Windows sports-betting analysis, paper-proof and controlled execution platform.**

Автоспорт — окремий від Nika-Core продукт. Він розробляється, тестується, пакується та запускається незалежно від готовності Nika-Core.

## Product intent

Autosport **не є permanently paper-only продуктом**. Його зріла продуктова мета — замкнути професійний цикл:

`lawful data -> pre-match/live analysis -> forecast -> multi-agent decision -> bankroll/portfolio risk -> multiple singles/parlays/positions -> paper proof -> bookmaker capability/account read -> supervised real execution -> real execution ledger/reconciliation -> bounded autonomous execution -> causal learning/adaptation`.

`REAL_MONEY_EXECUTION=false` означає лише, що поточна реалізація ще не має кваліфікованого real-money execution path. Це не постійна заборона продукту.

V1 навмисно використовує replay, Virtual Bank і paper betting, щоб без ризику реальних коштів довести causal correctness, прогнозування, stake sizing, portfolio/risk control, learning/evaluation, restart/recovery та Windows/NVDA usability. Після V1 і професійної paper profitability/safety qualification той самий продукт переходить до окремо керованої bookmaker execution програми в GitHub Issue #353.

## V1 executable path

Перший vertical slice — настільний теніс, але canonical domain має залишатися sport-generic. V1 — реальний Windows-продукт з агентами, high-speed Market Mirror, sealed historical replay без future leakage, Virtual Bank, singles/parlays, deterministic settlement, incremental Portfolio/Exposure Engine, evaluation/learning loop та keyboard/NVDA-oriented UI.

Швидкий ingestion/storage/portfolio math не залежить від LLM. AI працює над нормалізованими структурами й не замінює deterministic calculations.

У V1 реальні ставки **вимкнені**. Реальний bookmaker/account execution активується лише після окремих post-V1 доказів якості, ризик-контролю, прав/умов інтеграції та безпеки. Пріоритет інтеграцій: official API -> sanctioned integration -> permitted browser automation. Майбутній execution layer повинен підтримувати account/balance readback, event/market/selection verification, bet-slip preparation, stake entry, odds/slippage recheck, bookmaker acknowledgement, external bet ID, open/settled position readback, reconciliation і duplicate-bet prevention.

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

- `PRODUCT-WIDE` — canonical contracts, sport/market/event model, storage, replay, portfolio math, agents, learning/evaluation, observability, Windows/accessibility, provider interfaces and future bookmaker/execution interfaces.
- `V1-CRITICAL-PATH` — shortest path to the first runnable Windows proof release without money-moving execution.
- `POST-V1 EXECUTION` — professional paper qualification -> bookmaker capability/account read -> supervised execution -> real ledger/reconciliation -> bounded autonomy.

## Work labels / lane tags

`PRODUCT-FOUNDATION`, `V1-CRITICAL`, `V1-QA`, `DATA`, `REPLAY`, `PORTFOLIO`, `AGENTS`, `LEARNING`, `PROVIDER`, `PERFORMANCE`, `WINDOWS`, `ACCESSIBILITY`, `RELEASE`, `BOOKMAKER`, `EXECUTION`, `DOCS`.

## Canonical control

- Repository: `Oleksii-debug/Autosport`
- Global coordination: GitHub Issue #1
- Long-horizon bookmaker execution program: GitHub Issue #353
- Human-readable master specification: Google Drive folder `Автоспорт`, document `АВТОСПОРТ — MASTER TECHNICAL PROJECT`

## Product progression

- **V1 — Windows Paper/Replay Proof Release**
- **V1.0.x — Reliability / Bug Bash**
- **Professional Paper Qualification — long-running bankroll/risk/model proof**
- **Bookmaker Read-Only — capability, account/balance, limits, open/settled positions**
- **Supervised Execution — prepared bet + human confirmation**
- **Real Execution Ledger / Reconciliation**
- **Bounded Autonomous Execution — only inside explicit user limits and kill-switch policy**
- **Mathematical Intelligence / Multi-Sport / Multi-Provider expansion**

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`REAL_MONEY_EXECUTION=false`  
`V1_READY=false`
