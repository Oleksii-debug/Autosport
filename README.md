# Автоспорт / Autosport

**Independent agentic Windows sports-market research and paper-simulation laboratory.**

Автоспорт — окремий від Nika-Core продукт. Він розробляється, тестується, пакується та запускається незалежно від готовності Nika-Core.

## Product direction

Перший вертикальний зріз: настільний теніс. V1 — реальний Windows-продукт з агентами, high-speed Market Mirror, historical replay без future leakage, Virtual Bank, singles/parlays, deterministic settlement, incremental Portfolio/Exposure Engine, evaluation/learning loop та keyboard/NVDA-oriented UI.

Швидкий контур ingestion/storage/portfolio math не залежить від LLM. AI працює над уже нормалізованими структурами й не замінює deterministic calculations.

Real-money autonomous wagering не входить до scope. V1–V4 працюють як paper/replay research platform або формують аналітичні рекомендації для людини.

## Development model

Розробка йде одночасно у двох контурах:

- `PRODUCT-WIDE` — правильна довгострокова архітектура всього продукту.
- `V1-CRITICAL-PATH` — найкоротший шлях до першого готового Windows-релізу.

Main містить лише інтегровану технічну правду. Перед новою роботою треба перевіряти live main, open PRs, active owners, CI та найближчий V1 blocker.

## Work labels / lane tags

Використовувати в назвах issues/PRs один основний lane tag:

`PRODUCT-FOUNDATION`, `V1-CRITICAL`, `V1-QA`, `DATA`, `REPLAY`, `PORTFOLIO`, `AGENTS`, `LEARNING`, `PROVIDER`, `PERFORMANCE`, `WINDOWS`, `ACCESSIBILITY`, `RELEASE`, `DOCS`.

Один semantic slice — один source owner. Не створювати duplicate implementation, якщо canonical owner уже існує.

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
