# Автоспорт — Product Vision

## 1. Мета продукту

Автоспорт — незалежний Windows-продукт і багатоагентна лабораторія спортивного ринку для високошвидкісного збору та відтворення коефіцієнтів, paper-betting симуляції, детермінованого портфельного аналізу, прогнозування, оцінювання стратегій і навчання на причинно коректних історичних replay. Продукт розробляється незалежно від готовності Nika-Core; готові нейтральні компоненти з Nika-Core, Accessible Chess та інших lawful/open-source проєктів можуть вибірково переноситися або адаптуватися після перевірки сумісності.

Автоспорт не є просто чат-агентом. Швидкі числові операції, ринковий стан, залежності квитків, портфельні сценарії, settlement і ризик рахуються детермінованими програмними компонентами. AI-агенти працюють над дослідженням, прогнозами, постановкою гіпотез, вибором стратегій, критикою та навчанням, але не замінюють точну арифметику.

## 2. Режими роботи

Продукт має один спільний MarketEventStream contract для двох основних режимів: live-observation та historical replay. У live-observation детермінований provider/collector отримує доступні матчі, markets, selections, scores/status та odds і перетворює їх на нормалізовані часові події. У historical replay ті самі події відтворюються причинно, без доступу strategy agents до майбутнього результату; settlement відкриває фактичний outcome тільки тоді, коли replay-час досягає відповідної події.

Replay підтримує real-time, прискорений і event-driven режими. Якщо між двома змінами немає значущих подій, симулятор може переходити безпосередньо до наступної події, що дає можливість проганяти великі історичні масиви значно швидше за реальний час.

## 3. Високочастотний Market Mirror

Hot path не залежить від LLM: provider/source -> deterministic adapter/parser -> normalized MarketEvent -> in-memory current state -> append-only/time-series storage -> incremental mathematical engines. Дані мають підтримувати winner/moneyline, totals, handicaps/spreads, set/game markets та розширювану систему інших market types. Кожна зміна має точний timestamp, provider/source identity, match/market/selection identity та значення odds/status.

Система повинна масштабуватися на десятки одночасних матчів, сотні/тисячі активних selections і високочастотні зміни без повного перерахунку всієї моделі після кожного event.

## 4. Paper Book і портфель

Користувач задає віртуальний банк, наприклад 10 000 UAH. PaperBook створює віртуальні singles/ординари, parlays/експреси та складні портфелі, зберігаючи stake, odds snapshot, legs, timestamp, strategy version, reasoning/evidence references і статус. Settlement Engine проводить завершені tickets за правилами win/loss/void/refund та іншими підтриманими правилами без зміни історичних рішень заднім числом.

Portfolio/Exposure Engine є математичною пам'яттю всього портфеля. Він знає, які tickets залежать від кожного outcome, як зміна конкретної selection впливає на відкриті позиції, і обчислює worst-case, best-case, expected P&L, exposure, drawdown, risk-of-ruin та інші показники. Система ніколи не вважає прибуток гарантованим лише тому, що створено багато експресів: гарантія/hedge повинна бути доведена формально в межах визначеного набору сценаріїв.

## 5. Комбінаторика та швидкість

Простір terminal scenarios може бути астрономічним, тому повний brute-force enumeration не є базовою стратегією. Архітектура повинна підтримувати dependency/factor graphs, incremental invalidation, dynamic programming, branch-and-bound, dominance pruning, constraint solving, scenario compression, точний solver для контрольованих просторів і Monte Carlo/approximation для великих просторів із явною позначкою похибки.

Коли змінюється одна selection/odds, перераховується лише affected subgraph: dirty selections -> dependent candidate tickets -> dependent portfolio states -> updated opportunity/risk metrics.

## 6. Агентна лабораторія

Агенти можуть мати спеціалізовані ролі: research, player/match analysis, forecast, ticket construction, portfolio/risk, critic, settlement review, learning/evaluation. Ролі не повинні створювати дубльовану інфраструктуру; усі працюють через спільні canonical stores/contracts. Окремий collector не повинен бути LLM-агентом у hot path: його робота детермінована й високошвидкісна.

Learning records фіксують рішення до outcome: доступні дані, probabilities, features, odds, candidate tickets, обраний/відхилений action, strategy/model version та risk snapshot. Це забезпечує чесне post-settlement evaluation та захист від future leakage.

## 7. Оцінювання

Продукт відстежує не лише зміну банку, а ROI, max drawdown, volatility, risk of ruin, calibration, Brier/log-loss, результати за market type, pre-match/live, favorites/underdogs, singles/parlays, walk-forward/out-of-sample performance та результат після виключення найбільших одиничних lucky wins. Жоден короткий прибутковий період сам по собі не є доказом стабільної переваги.

## 8. Windows і доступність

Автоспорт є Windows-продуктом із web-style WebView UI, оптимізованим для keyboard-only та NVDA. Сторінки мають семантичні landmarks, headings, tables/lists там, де це доречно, передбачуваний focus order та стандартні browser/editing shortcuts.

Критичний контракт: будь-який основний зміст, який NVDA озвучує користувачу, одночасно існує як звичайний видимий selectable/copyable DOM text. `aria-live`, alerts та accessibility-only nodes використовуються лише як дубльовані короткі notifications, але не як єдине місце з корисними даними. Заборонено глобально блокувати selection/copy або перехоплювати Ctrl+C/Ctrl+A у звичайному контенті й editable controls. Цей контракт має автоматизовані regression tests.

## 9. Релізи та whole-product development

Версії є milestones, а не ізольованими фазами. Команда може паралельно розвивати ingestion, replay, portfolio, agents, Windows UI, performance та packaging, якщо це не створює конфліктів. Перший release candidate повинен уже бути цілісним Windows-продуктом із агентами, а не throwaway demo.

Орієнтовні milestones: v0.1 — runnable vertical slice з causal replay, paper bankroll, базовими tickets, semantic UI та deterministic tests; v0.2 — високочастотний store, розширені markets, portfolio exposure та incremental recomputation; v0.3 — multi-agent research/forecast/critic/learning, evaluation lab і масштабний replay; v0.4 — optimized combinatorial engine, long-run experiments, live-observation adapters і production-like resilience; v1.0 — стабільний packaged Windows product із перевіреною доступністю, performance, recovery та reproducible release evidence.

## 10. Межа реального wagering

Canonical product scope на цьому етапі — paper/simulation, historical replay, live-observation/analysis, forecasting, portfolio/risk та human-review decision support. Автономне виконання ставок реальними грошима не входить до реалізованого execution path; real-money executor відсутній/disabled.

## 11. Головний критерій готовності

Автоспорт вважається готовим не через кількість модулів або PR, а коли packaged Windows application дозволяє користувачу клавіатурою/NVDA завантажити або отримати market stream, запустити causal replay/live observation, бачити копійований ринковий стан, керувати віртуальним банком, запускати агентні стратегії, отримувати детерміновані portfolio calculations, проводити settlement, оцінювати результати, відновлювати стан після restart і повторювати експерименти відтворювано.