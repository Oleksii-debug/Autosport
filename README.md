# Автоспорт

**Автоспорт** — незалежний Windows-продукт і багатоагентна лабораторія спортивного ринку для високочастотної історії коефіцієнтів, причинного historical replay, paper bankroll/tickets, портфельної математики, прогнозування, evaluation/learning та доступного keyboard/NVDA інтерфейсу.

## Поточний статус

Проєкт стартував як whole-product development: версії є release checkpoints, але ingestion, replay, PaperBook, portfolio, agents, UI, persistence, performance і packaging можуть розвиватися паралельно. Перший bootstrap branch уже містить canonical domain events, causal replay, exact-decimal paper ledger, semantic copyable web UI та cross-platform CI.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`REAL_MONEY_EXECUTION=false`

## Головні документи

- `AGENTS.md` — правила автономної розробки, reuse-first, anti-duplication, causal/accessibility invariants.
- `docs/PRODUCT_VISION.md` — продуктова мета.
- `docs/TECHNICAL_PROJECT.md` — canonical технічний проєкт.
- `docs/ROADMAP.md` — release checkpoints v0.1 → v1.0 при whole-product development.
- `docs/ACCESSIBILITY_CONTRACT.md` — keyboard/NVDA і обов’язково видимий/selectable/copyable текст.
- `docs/REUSE_MATRIX.md` — що беремо/адаптуємо з Nika-Core, Accessible Chess та open-source.
- `docs/PROGRESS.md` — evidence-based прогрес 0–100%.
- `docs/CONTINUATION_PROMPT.md` — повний A–Z prompt для продовження в іншому чаті.

## Ключова архітектура

Hot path: `provider -> deterministic adapter -> MarketEvent -> event store/current state -> affected dependency graph -> incremental mathematics`. LLM не стоїть між кожною зміною коефіцієнта й математичним станом. Live-observation і historical replay подаються в один downstream contract; historical strategy не бачить future outcome до causal release time.

PaperBook використовує точну decimal-арифметику. Portfolio/Exposure Engine має рахувати сукупний ризик і сценарії всіх tickets, а не покладатися на пам’ять мовної моделі. Для великих комбінацій плануються dependency graphs, DP/branch-and-bound/pruning/constraint solver adapters і sampling/Monte Carlo там, де exact enumeration неможливий.

## Межа продукту

Canonical implementation — paper/simulation, historical replay, permitted live observation/analysis, forecasting, portfolio/risk та human-review support. Автономний real-money wagering executor не входить до реалізованого execution path.
