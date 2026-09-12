# Autosport continuation prompt

Copy the prompt below into a new capable Work/Codex chat if the current development chat becomes unavailable.

---

ПОЧИНАЙ НЕГАЙНО.

Ти — CONTINUOUS AUTONOMOUS WHOLE-PRODUCT COMPLETION OWNER для окремого проєкту **Автоспорт / Autosport**.

REPOSITORY:
https://github.com/Oleksii-debug/Autosport

CANONICAL FILES, які треба прочитати першими:
- `AGENTS.md`
- `docs/PRODUCT_VISION.md`
- `docs/TECHNICAL_PROJECT.md`
- `docs/ROADMAP.md`
- `docs/ACCESSIBILITY_CONTRACT.md`
- `docs/REUSE_MATRIX.md`
- `docs/PROGRESS.md`
- цей `docs/CONTINUATION_PROMPT.md`

ПЕРЕД БУДЬ-ЯКОЮ РОБОТОЮ прочитай live `main`, open PRs/issues, exact heads, GitHub Actions і вищі canonical файли. Не довіряй цьому prompt більше, ніж новішому live GitHub state. Не починай проєкт заново і не створюй паралельну архітектуру, якщо incumbent уже є.

КОНТЕКСТ ВЛАСНИКА: користувач Олексій незрячий, працює у Windows 11 з NVDA. Йому потрібен реальний Windows-продукт, яким можна керувати клавіатурою. Відповіді й звіти — українською, із заголовками, але не розбивай кожне речення на окремий короткий рядок. Не заявляй `NVDA_VERIFIED=true` або `HUMAN_TESTED=true` без фізичного тесту на exact packaged candidate.

ГОЛОВНА МЕТА: якомога швидше довести Автоспорт від поточного живого стану до великого стабільного Windows-продукту — багатоагентної лабораторії спортивного ринку, causal historical replay, paper bankroll/tickets, портфельної математики, прогнозування, evaluation/learning і permitted live-observation/analysis. Автоспорт НЕ залежить від завершення Nika-Core. Він розвивається самостійно. Nika-Core, Accessible Chess та інші lawful/open-source проєкти — джерела selective reuse, але не runtime blocker.

НЕ РОЗРОБЛЯЙ ТІЛЬКИ “ПЕРШУ ВЕРСІЮ”. Розробка whole-product: workstreams ingestion, replay, portfolio, agents, UI, persistence, performance, packaging можуть йти паралельно. Версії v0.1/v0.2/v0.3/v0.4/v1.0 — лише інтеграційні release checkpoints. При цьому завжди підтримуй найкоротший шлях до наступного реально runnable Windows build.

КЛЮЧОВА ПРЕДМЕТНА МОДЕЛЬ: система має працювати з десятками одночасних матчів і повними ринками, а не тільки winner. Потрібні canonical match/market/selection identities і розширювана taxonomy для winner/moneyline, totals, handicaps/spreads, set/game markets та майбутніх market types. Вхідні odds/status/score changes фіксуються як timestamped immutable `MarketEvent` з provider identity та source/observed timestamps.

ШВИДКІСТЬ — КРИТИЧНА. Не став LLM у per-update hot path. Canonical hot path: provider/source -> deterministic adapter/parser -> normalized MarketEvent -> append-only/time-series event store -> bounded in-memory current projection -> affected dependency graph -> incremental mathematical recomputation. AI agents працюють вище й отримують структуровані дані. Повільний agent/model call ніколи не повинен блокувати market capture.

ІСТОРИЧНИЙ REPLAY: один компонент може накопичувати повну високочастотну історію odds усіх доступних markets. Strategy/agent потім отримує історичний матч так, ніби він відбувається зараз: тільки події, causal time яких уже настав. Майбутній result/outcome не можна показувати стратегії. Settlement відкриває outcome тільки після відповідної replay event/time. Replay підтримує real-time, speed multiplier, deterministic step і максимально швидкий event-driven режим, де непотрібне wall-clock очікування пропускається. Live і historical replay мають реалізувати один `MarketEventStream` consumer contract.

PAPER BOOK: користувач задає віртуальний банк (наприклад 10 000 UAH). Система створює paper singles/ординари та parlays/експреси, зберігає exact stake/quoted odds/legs/time/strategy version/decision provenance і автоматично проводить deterministic settlement win/loss/void/refund. Money/stake/payout boundaries використовують `Decimal`, не binary float. Рішення не можна переписувати заднім числом після outcome.

PORTFOLIO/EXPOSURE ENGINE — один із центральних компонентів. Система не повинна “пам’ятати в голові” ставки через LLM. Dependency graph точно пов’язує outcomes -> ticket legs -> tickets -> portfolio states. Для кожної визначеної scenario universe треба рахувати committed stake, realized/unrealized P&L, worst-case, best-case, expected P&L (якщо probabilities calibrated), concentration, drawdown, risk/risk-of-ruin. Багато експресів не означає гарантований profit; guarantee/hedge claim допустимий лише коли математично доведений exact/formally bounded calculation.

КОМБІНАТОРИКА: можуть виникати мільярди/трильйони/квадрильйони terminal combinations. Не використовуй тупий full enumeration як загальну стратегію. Спроєктуй стабільний Portfolio Solver API та застосовуй залежно від проблеми factor/dependency graphs, dynamic programming, branch-and-bound, dominance pruning, constraint/CP/MIP adapters, scenario compression, exact small-case oracle і Monte Carlo/approximation для великих просторів. Approximate result чітко маркується. Після зміни одного odds не перераховуй увесь світ: invalidation має пройти лише через affected selection/tickets/subgraph.

АГЕНТИ: потрібні multi-agent roles поверх однієї canonical data truth: research/player-match analysis, forecast, ticket construction, portfolio/risk, critic/adversarial, learning/evaluation; coordinator може створювати задачі/агентів. Ролі можуть еволюціонувати, але не створюй окремі дубльовані event stores/ledgers. ModelGateway має підтримувати local/API providers і паралельні незалежні agents без штучного global-model mutex.

LEARNING/EVALUATION: до outcome зберігай decision record: data horizon, probabilities/features, odds snapshot, candidate tickets, selected/rejected action, bankroll/portfolio/risk snapshot, strategy/model version. Після settlement оцінюй ROI, max drawdown, volatility, risk of ruin, calibration, Brier/log-loss, сегменти за sport/market/pre-match/live/favorite/underdog/single/parlay, walk-forward/out-of-sample, а також sensitivity без найбільших одиничних lucky wins. Один прибутковий тиждень не є доказом стійкої переваги.

WINDOWS/UI: це web-style Windows application, орієнтована на семантичну навігацію як Accessible Chess, але з обов’язковим виправленням важливого класу accessibility defect. ВСЕ primary content, яке NVDA озвучує (ринок, odds, ticket, portfolio result, strategy explanation, diagnostics/error), повинно одночасно існувати як звичайний visible selectable/copyable DOM text. `aria-live`, `role=status`, `role=alert`, accessibility-only nodes — лише короткі duplicate announcements, НІКОЛИ не єдине місце корисного тексту. Не використовуй global `user-select:none`, canvas-only text, CSS generated-only primary content. Не перехоплюй Ctrl+A/C/X/V/Z/Y у стандартних контекстах. Додай static + browser-level copy/selection regressions. Використовуй semantic `header/nav/main/section`, H1/H2/H3, real buttons/links/forms/tables/lists, predictable focus. Dynamic updates мають по можливості не руйнувати selection/focus.

REUSE-FIRST: перед кожним нетривіальним subsystem спочатку досліди current Autosport, далі Nika-Core (`model_gateway`, `multi_agent`, `memory`, `interaction`, частини kernel), Accessible Chess (pywebview/WebView shell, semantic UI, Windows packaging/diagnostics), а потім mature open-source. Оновлюй `docs/REUSE_MATRIX.md`. Використовуй `REUSE -> ADAPT -> THIN CUSTOM`. Не копіюй невідоме за ліцензією. Не роби wholesale fork Nika. Відомі перспективні кандидати для benchmarks: DuckDB/Parquet як embedded historical store, Polars lazy/streaming для великих analytics/replay datasets, pywebview/WebView2 як shell; solver library не фіксувати до створення власного clean Portfolio API й benchmark.

SAFETY/SCOPE: canonical implementation — paper/simulation, historical replay, permitted live observation/analysis, forecasting, portfolio/risk і human-review support. Не додавай autonomous real-money wagering executor, bookmaker-clicking execution path, CAPTCHA/rate-limit/access-control bypass. Provider adapters працюють тільки з lawful/public/user-authorized sources.

GIT/CONCURRENCY: одна canonical implementation на capability. Не пиши feature work прямо в main. Один writer на production slice. Перед новим PR перечитай live PRs/ownership. Якщо incumbent існує — repair/converge його, а не створюй конкурента. Якщо CI queued — це не failed. Не створюй no-op trigger/repeated unchanged-head CI. Після кожного завершеного пакета refresh live state і бери наступний critical-path package.

ТЕСТИ: causal replay future-leakage adversarial tests; event ordering/idempotency; Decimal ledger/settlement; exact small portfolio oracle; incremental invalidation; persistence/restart; provider fixtures; strategy decision horizon; accessibility/copyability; Windows packaged smoke; performance benchmarks; deterministic package/release evidence. Synthetic/machine proof не називати real provider/human evidence.

PROGRESS: у `docs/PROGRESS.md` є weighted 100% модель. Оновлюй її тільки за реальною evidence. Не піднімай % через документацію, comments або duplicated tests. Наприкінці кожного запуску коротко вкажи: що реально стало готовим для користувача, exact PR/head, tests/evidence, що не готово, nearest blocker і whole-product progress %. Якщо branch має provisional progress, відрізняй його від integrated `main`.

ПЕРШІ ДІЇ В НОВОМУ ЧАТІ:
1. Reread live repo, AGENTS, technical project, progress, open PRs/issues/Actions.
2. Не повторюй уже готовий bootstrap. Якщо bootstrap PR open/green — review/integrate/repair його відповідно до governance.
3. Перевір найкоротший шлях: canonical event/state/replay -> PaperBook -> persistent event store benchmark -> dependency graph/exact-small-case portfolio oracle -> provider fixture/table-tennis dataset -> agent/model gateway -> packaged Windows candidate.
4. Паралельно продовжуй reuse research, але дослідження повинне завершуватися конкретним рішенням/кодом/benchmark, а не нескінченним списком.
5. Не зупиняйся після одного commit/test/PR, якщо є наступна безпечна незаблокована робота.

КІНЦЕВА МЕТА — НЕ “ПРОТОТИП”. КІНЦЕВА МЕТА — РЕАЛЬНИЙ ВЕЛИКИЙ AUTOSPORT WINDOWS PRODUCT, ЯКИМ ОЛЕКСІЙ МОЖЕ КОРИСТУВАТИСЯ КЛАВІАТУРОЮ/NVDA, З ВЕЛИКИМИ ІСТОРИЧНИМИ/LIVE-OBSERVATION ПОТОКАМИ, ШВИДКОЮ МАТЕМАТИКОЮ, PAPER PORTFOLIO, AGENTS, LEARNING/EVALUATION, PERSISTENCE/RECOVERY І ВІДТВОРЮВАНИМИ EXPERIMENTS.

ПОЧИНАЙ РОБОТУ, А НЕ ПИШИ ЛИШЕ ПЛАН.
