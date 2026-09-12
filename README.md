# Автоспорт / Autosport

**Independent agentic Windows sports-market research and paper-simulation laboratory.**

Автоспорт — окремий від Nika-Core продукт. Він розробляється, тестується, пакується та запускається незалежно від готовності Nika-Core.

## V1 executable path

Перший vertical slice — настільний теніс. V1 — реальний Windows-продукт з агентами, high-speed Market Mirror, sealed historical replay без future leakage, Virtual Bank, singles/parlays, deterministic settlement, incremental Portfolio/Exposure Engine, evaluation/learning loop та keyboard/NVDA-oriented UI.

Швидкий ingestion/storage/portfolio math не залежить від LLM. AI працює над нормалізованими структурами й не замінює deterministic calculations.

Real-money autonomous wagering не входить до scope. Поточний продукт — paper/replay research platform та human-facing analytics.

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

- `PRODUCT-WIDE` — canonical contracts, market/event model, storage, replay, portfolio math, agents, observability, Windows/accessibility, provider interfaces.
- `V1-CRITICAL-PATH` — shortest path to first runnable Windows release.

## Work labels / lane tags

`PRODUCT-FOUNDATION`, `V1-CRITICAL`, `V1-QA`, `DATA`, `REPLAY`, `PORTFOLIO`, `AGENTS`, `LEARNING`, `PROVIDER`, `PERFORMANCE`, `WINDOWS`, `ACCESSIBILITY`, `RELEASE`, `DOCS`.

## Canonical control

- Repository: `Oleksii-debug/Autosport`
- Global coordination: GitHub Issue #1
- Human-readable master specification: Google Drive folder `Автоспорт`, document `АВТОСПОРТ — MASTER TECHNICAL PROJECT`

## Version roadmap

- **V1 — Windows Paper Lab / Table Tennis First**
- **V1.1 — Reliability and Performance**
- **V2 — Multi-Sport / Multi-Provider Portfolio Lab**
- **V3 — Learning Laboratory**
- **V4 — Scaled Autonomous Research Platform**

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`REAL_MONEY_EXECUTION=false`
