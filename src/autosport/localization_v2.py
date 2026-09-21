from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


DEFAULT_LOCALE = "uk-UA"
CATALOG_VERSION = 2

_UK_UA = MappingProxyType(
    {
        "ui.boolean.true": "так",
        "ui.boolean.false": "ні",
        "ui.app.title": "Автоспорт — аналітична програма для Windows",
        "ui.dialog.title": "Автоспорт",
        "ui.label.strategy": "Стратегія:",
        "ui.label.speed": "Швидкість:",
        "ui.label.live_mode": "Режим живого спостереження:",
        "ui.label.live_quotes": "Поточні котирування",
        "ui.label.tickets": "Паперові квитки і результати",
        "ui.label.evaluation": "Оцінювання та докази портфеля",
        "ui.label.log": "Журнал",
        "ui.button.research_plan": "Вибрати план дослідження",
        "ui.button.choose_dataset": "Вибрати набір даних",
        "ui.button.run_replay": "Запустити паперовий повтор",
        "ui.button.repair_workspace": "Відновити робочу область",
        "ui.button.live_refresh": "Оновити поточний знімок",
        "ui.speed.event_driven": "Подієвий — максимально швидко",
        "ui.speed.realtime": "1× реальний час",
        "ui.speed.10x": "10×",
        "ui.speed.100x": "100×",
        "ui.speed.1000x": "1000×",
        "ui.live_mode.public_preview": "Публічний перегляд — без ключа",
        "ui.live_mode.api_key": "Ключ API із середовища",
        "ui.strategy.display": "Стратегія {strategy_id}",
        "ui.strategy.label.baseline-v1": "Базова стратегія тестового сценарію",
        "ui.strategy.label.observe-only-v1": "Лише спостереження",
        "ui.strategy.label.research-replay-v1": "Типізований дослідницький повтор",
        "ui.error.strategy.unknown_display": "Невідома канонічна стратегія: {display}",
        "ui.error.exception.message_unavailable": "{exception_type}: <повідомлення недоступне>",
        "ui.status.startup.ready": (
            "Готово. Виберіть папку набору даних для повтору або оновіть поточний знімок."
        ),
        "ui.status.startup.recovery_required": (
            "Економічний сеанс не пройшов перевірку запуску; паперовий повтор для базової "
            "робочої області заблоковано до відновлення. Поточний знімок лише для читання "
            "доступний через Control+L."
        ),
        "ui.status.dataset.none": "Набір даних не вибрано.",
        "ui.status.research_plan.baseline": "План дослідження: для baseline-v1 не потрібен.",
        "ui.status.live.never": "Поточний знімок ще не завантажувався.",
        "ui.status.live_quotes.empty": "Поточні котирування ще відсутні.",
        "ui.status.evaluation.empty": "Оцінювання ще відсутнє. Запустіть паперовий повтор.",
        "ui.strategy.plan.required": "план дослідження обов’язковий",
        "ui.strategy.plan.not_required": "план дослідження не потрібен",
        "ui.strategy.ticket.opens": "може відкривати паперові квитки",
        "ui.strategy.ticket.observe_only": "паперові квитки не відкриває",
        "ui.strategy.status": (
            "Стратегія: {strategy_id}; {label}; {plan_requirement}; {ticket_mode}. "
            "Економічна історія однієї робочої області не змішується між різними "
            "ідентичностями стратегії/плану."
        ),
        "ui.status.strategy.dataset_busy": (
            "Конфігурацію стратегії не можна змінювати під час перевірки набору даних."
        ),
        "ui.status.research_plan.not_required": "План дослідження: не потрібен для {strategy_id}.",
        "ui.status.research_plan.required_missing": "План дослідження: обов’язковий; файл ще не вибрано.",
        "ui.status.strategy.selected": "Вибрано канонічну стратегію {strategy_id}.",
        "ui.status.research_plan.dataset_busy": (
            "План дослідження не можна змінювати під час перевірки набору даних."
        ),
        "ui.status.research_plan.replay_busy": "План дослідження не можна змінювати під час економічного повтору.",
        "ui.info.research_plan.not_supported": (
            "{strategy_id} не використовує план дослідження. Виберіть research-replay-v1."
        ),
        "ui.status.research_plan.not_required_short": "{strategy_id}: план дослідження не потрібен.",
        "ui.dialog.research_plan.choose_title": "Вибрати план дослідження Autosport",
        "ui.filetype.json": "Файли JSON",
        "ui.filetype.all": "Усі файли",
        "ui.error.research_plan.rejected": "План дослідження відхилено: {detail}",
        "ui.status.research_plan.validation_failed": (
            "План дослідження не змінено: файл не пройшов закриту при помилці перевірку."
        ),
        "ui.status.research_plan.selected": "План дослідження: {name}; SHA-256={sha256}",
        "ui.status.research_plan.bound": (
            "План дослідження перевірено і прив’язано до {strategy_id}; SHA-256={sha_short}…"
        ),
        "ui.status.research_plan.identity_suffix": "; план={sha_suffix}",
        "ui.status.bank.recovery_required": (
            "Віртуальний банк: недоступний до успішного відновлення; робоча область: {workspace}"
        ),
        "ui.status.bank.pending": "Віртуальний банк: оновлюється після повтору; робоча область: {workspace}",
        "ui.status.bank.current": (
            "Віртуальний банк: {balance}; зарезервовано: {committed_stake}; "
            "стратегія: {strategy_id}; робоча область: {workspace}"
        ),
        "ui.status.bank.quarantined": (
            "Віртуальний банк: недоступний до підтвердженого завершального стану/відновлення; "
            "робоча область: {workspace}"
        ),
        "ui.status.dataset.validation_busy": "Перевірка набору даних уже виконується; дочекайтеся завершального результату.",
        "ui.status.dataset.replay_busy": (
            "Повтор уже виконується; вибір іншого набору даних доступний після завершення поточного запуску."
        ),
        "ui.status.dataset.live_busy": (
            "Поточний знімок уже виконується; вибір набору даних доступний після завершального стану живого спостереження."
        ),
        "ui.status.dataset.recovery_busy": (
            "Відновлення робочої області вже виконується; вибір набору даних доступний після його завершального стану."
        ),
        "ui.log.session.teardown_secondary": (
            "Завершення економічного сеансу після карантину завершилося помилкою; "
            "робоча область={workspace}; вторинна_помилка={detail}"
        ),
        "ui.dialog.dataset.choose_title": "Вибрати папку набору даних для повтору Autosport",
        "ui.error.dataset.worker_not_started": (
            "Процес перевірки набору даних не запущено; попередній перевірений набір даних не змінено. "
            "Повторіть вибір після завершення поточних операцій."
        ),
        "ui.status.dataset.validation_running": (
            "Перевірка набору даних виконується у фоновому процесі лише для читання: {path}. "
            "Потік Tk/UIA/NVDA і журнал залишаються доступними."
        ),
        "ui.log.dataset.validation_started": "Фонову перевірку набору даних запущено: {path}",
        "ui.error.dataset.rejected": "Набір даних відхилено: {detail}",
        "ui.status.dataset.validation_failed": (
            "Набір даних не змінено: фонова закрита при помилці перевірка завершилася помилкою."
        ),
        "ui.error.dataset.identity_mismatch": (
            "Набір даних відхилено: завершальний результат процесу не відповідає точній вибраній папці; "
            "попередній перевірений набір даних не змінено."
        ),
        "ui.status.dataset.identity_mismatch": "Набір даних не змінено: невідповідність ідентичності завершальної перевірки.",
        "ui.status.dataset.summary": (
            "Набір даних: {name}; спорт={sport}; SHA ринку={market_sha}; SHA запечатаних результатів={results_sha}"
        ),
        "ui.status.dataset.ready": "Набір даних перевірено. Можна запускати повтор.",
        "ui.log.dataset.validation_success": (
            "Набір даних перевірено у фоновому процесі: {name}; спорт={sport}; корінь={root}"
        ),
        "ui.status.live.dataset_busy": "Поточний знімок відкладено: перевірка набору даних ще виконується.",
        "ui.status.live.dataset_blocks": (
            "Живе спостереження лише для читання не запускається одночасно з перевіркою набору даних."
        ),
        "ui.status.live.replay_busy": (
            "Поточний знімок відкладено: економічний повтор уже виконується у цій робочій області."
        ),
        "ui.status.live.unknown_mode": "Невідомий режим живого спостереження; знімок не запущено.",
        "ui.error.live.api_key_missing": (
            "AUTOSPORT_PARLAYAPI_KEY не задано в середовищі; виберіть публічний перегляд або задайте ключ до запуску програми"
        ),
        "ui.status.live.busy": "Поточний знімок уже виконується; дочекайтеся завершення поточного запиту.",
        "ui.status.live.running": "Поточний знімок виконується у фоновому процесі; інтерфейс залишається доступним.",
        "ui.status.live.read_only_running": "Виконується живе спостереження лише для читання. Паперовий облік не змінюється.",
        "ui.error.live.snapshot": "Помилка поточного знімка: {detail}",
        "ui.status.live.failed": "Живе спостереження не оновлено; стан повтору/паперовий стан не змінено.",
        "ui.status.live.no_result": "Процес живого спостереження завершився без результату.",
        "ui.status.live.updated": "Поточний знімок лише для читання оновлено.",
        "ui.status.windows.economic_unavailable": (
            "Економічний стан тимчасово недоступний; дочекайтеся завершення повтору або відновлення."
        ),
        "ui.status.windows.strategy_recovery_busy": (
            "Конфігурацію стратегії заблоковано: відновлення робочої області ще виконується."
        ),
        "ui.status.windows.research_plan_recovery_busy": "План дослідження не можна змінювати під час відновлення робочої області.",
        "ui.status.windows.dataset_recovery_busy": "Набір даних не можна змінювати під час відновлення робочої області.",
        "ui.status.windows.live_recovery_busy": "Поточний знімок відкладено: відновлення робочої області ще виконується.",
        "ui.status.windows.live_recovery_blocked": (
            "Живе спостереження лише для читання не запускається одночасно з відновленням робочої області."
        ),
        "ui.status.recovery.dataset_busy": "Відновлення заблоковано: перевірка набору даних ще виконується.",
        "ui.status.recovery.replay_busy": "Відновлення заблоковано: економічний повтор ще виконується.",
        "ui.status.recovery.live_busy": "Відновлення заблоковано: поточний знімок ще виконується.",
        "ui.status.recovery.already_busy": "Відновлення робочої області вже виконується; другий процес відновлення не запущено.",
        "ui.error.recovery.configuration": "Конфігурацію відновлення відхилено: {detail}",
        "ui.status.recovery.configuration_rejected": (
            "Відновлення не запущено: канонічна конфігурація стратегії не пройшла закриту при помилці перевірку."
        ),
        "ui.status.recovery.in_progress_ticket": (
            "Відновлення робочої області виконується; стан економічного сеансу недоступний до завершення перевірки."
        ),
        "ui.error.recovery.teardown": (
            "Відновлення робочої області відхилено закрито при помилці: "
            "не вдалося завершити попередній економічний сеанс; {detail}"
        ),
        "ui.error.recovery.teardown_no_reopen": (
            "Відновлення робочої області відхилено закрито при помилці: не вдалося завершити "
            "попередній економічний сеанс; узгодження та повторне відкриття не запускаються."
        ),
        "ui.status.recovery.teardown_blocked": (
            "Відновлення робочої області не запущено: не вдалося завершити попередній економічний сеанс; "
            "економічний стан лишається прихованим, а робочу область заблоковано закрито при помилці."
        ),
        "ui.status.recovery.state_unavailable": "Відновлення робочої області не завершено; стан економічного сеансу недоступний.",
        "ui.error.recovery.failure": "Відновлення робочої області відхилено закрито при помилці: {detail}",
        "ui.status.recovery.failed_until_fixed": (
            "Відновлення робочої області не завершено; економічний стан лишається недоступним, а новий повтор заблоковано до усунення причини."
        ),
        "ui.status.recovery.start_failed": (
            "Відновлення робочої області не запущено; стан сеансу лишається закритим при помилці до повторного успішного відновлення."
        ),
        "ui.status.recovery.running": (
            "Відновлення робочої області виконується у фоновому процесі; стратегія={strategy_id}{plan_identity}. "
            "Потік Tk/UIA/NVDA лишається доступним; повтор, живе спостереження, відновлення та елементи "
            "конфігурації заблоковано до завершального стану."
        ),
        "ui.log.recovery.started": (
            "Відновлення робочої області запущено у фоновому процесі; стратегія={strategy_id}{plan_identity}; "
            "робоча область={workspace}."
        ),
        "ui.error.recovery.worker": "Відновлення робочої області відхилено закрито при помилці: {detail}",
        "ui.status.recovery.blocked": (
            "Відновлення робочої області не завершено; цю економічну робочу область заблоковано для нового повтору до успішного відновлення."
        ),
        "ui.status.recovery.no_result": (
            "Процес відновлення робочої області завершився без завершального результату; ця робоча область лишається закритою при помилці."
        ),
        "ui.error.recovery.identity_mismatch": (
            "Невідповідність ідентичності завершального результату відновлення: очікувана робоча область={expected_workspace}, "
            "стратегія={expected_strategy_id}; отримана робоча область={received_workspace}, стратегія={received_strategy_id}."
        ),
        "ui.status.recovery.identity_mismatch": (
            "Завершальний результат відновлення не відповідає запущеній економічній робочій області/стратегії; "
            "стан лишається закритим при помилці й жодну робочу область не розблоковано."
        ),
        "ui.recovery.summary": (
            "Відновлення робочої області: узгоджено={reconciled}; перервано_незафіксованих={aborted_uncommitted}; "
            "невирішених={unresolved}; робоча область={workspace}"
        ),
        "ui.status.recovery.unresolved_ticket": "Відновлення робочої області має невирішений запуск; стан економічного сеансу недоступний.",
        "ui.status.recovery.unresolved_suffix": (
            ". Є невирішений успадкований запуск без достатнього доказу підсумку; економічний стан лишається прихованим, "
            "а повтор закритим при помилці для цієї робочої області."
        ),
        "ui.warning.recovery.unresolved": (
            "Відновлення завершило перевірку, але залишило невирішений запуск без достатнього доказу завершення. "
            "Економічний стан не публікується; не обходьте цей стан через allow-repeat."
        ),
        "ui.status.recovery.reopen_validation": (
            "Відновлення завершено, але стан економічного сеансу не пройшов перевірку повторного відкриття."
        ),
        "ui.error.recovery.reopen": "Повторне відкриття робочої області після відновлення відхилено закрито при помилці: {detail}",
        "ui.status.recovery.reopen_blocked": (
            "Узгодження відновлення завершено, але стан економічного сеансу лишається недоступним; новий повтор "
            "заблоковано до успішного відновлення/повторного відкриття."
        ),
        "ui.status.recovery.ready_suffix": ". Робоча область готова до наступного перевіреного паперового повтору.",
        "ui.info.recovery.complete": "Відновлення робочої області завершено без невирішених запусків.",
        "ui.status.replay.dataset_busy": "Паперовий повтор не запускається: перевірка набору даних ще виконується.",
        "ui.info.replay.dataset_required": "Спочатку виберіть набір даних.",
        "ui.status.replay.already_busy": "Паперовий повтор уже виконується; дочекайтеся його завершального стану.",
        "ui.status.replay.live_busy": "Поточний знімок ще виконується; паперовий повтор почнеться лише після його завершення.",
        "ui.error.replay.configuration": "Конфігурацію стратегії відхилено: {detail}",
        "ui.status.replay.configuration_rejected": (
            "Повтор не запущено: канонічна конфігурація стратегії не пройшла закриту при помилці перевірку."
        ),
        "ui.status.replay.recovery_busy": "Паперовий повтор не запускається: відновлення робочої області ще виконується.",
        "ui.status.replay.recovery_required": (
            "Паперовий повтор заблоковано закрито при помилці: поточна робоча область не має успішного завершального результату відновлення."
        ),
        "ui.status.replay.quarantined": (
            "Паперовий повтор заблоковано: ця економічна робоча область має непідтверджений завершальний стан. "
            "Виконайте «Відновити робочу область» або Control+Shift+R; новий запуск дозволяється лише після "
            "успішного відновлення без невирішених запусків."
        ),
        "ui.status.replay.preparing_ticket": (
            "Паперовий повтор готується; стан попереднього економічного сеансу приховано до підтвердженого переходу."
        ),
        "ui.error.replay.teardown": (
            "Паперовий повтор не запущено закрито при помилці: не вдалося завершити попередній економічний сеанс; "
            "цільовий повтор не стартував, а точна проблемна робоча область потребує відновлення."
        ),
        "ui.status.replay.teardown_blocked": (
            "Паперовий повтор не запущено: не вдалося завершити попередній економічний сеанс; застарілий "
            "економічний стан приховано, а проблемну робочу область заблоковано до відновлення."
        ),
        "ui.status.replay.start_failed": "Паперовий повтор уже виконується; новий запуск не запущено.",
        "ui.evaluation.running": (
            "Повтор виконується; оцінювання оновиться лише після завершальної межі розрахунку результатів/оцінювання."
        ),
        "ui.status.replay.running": (
            "Повтор виконується у фоновому процесі; стратегія={strategy_id}{plan_identity}. Клавіатура, фокус, "
            "F6/F7/F8 і журнал залишаються доступними; закриття програми заблоковано до завершення економічної транзакційної межі."
        ),
        "ui.log.replay.started": (
            "Паперовий повтор запущено у фоновому процесі; стратегія={strategy_id}{plan_identity}; "
            "робоча область={workspace}; потік Tk/UIA не блокується."
        ),
        "ui.status.replay.error_ticket": "Повтор завершився помилкою; стан економічного сеансу недоступний до відновлення.",
        "ui.error.replay.worker": "Помилка паперового повтору: {detail}",
        "ui.evaluation.replay_failed": (
            "Оцінювання недоступне: повтор не досяг завершальної межі розрахунку результатів та оцінювання."
        ),
        "ui.status.replay.failed_recovery": (
            "Повтор завершився помилкою; економічний стан цієї робочої області лишається прихованим до відновлення. "
            "Виконайте «Відновити робочу область» або Control+Shift+R перед наступним повтором."
        ),
        "ui.status.replay.no_result_ticket": (
            "Процес повтору не повернув завершальний результат; стан економічного сеансу недоступний до відновлення."
        ),
        "ui.evaluation.no_terminal_result": "Оцінювання недоступне: процес не повернув завершальний SessionResult.",
        "ui.status.replay.no_terminal_result": (
            "Процес повтору завершився без завершального результату; економічний стан цієї робочої області "
            "лишається прихованим до відновлення. Виконайте «Відновити робочу область» перед наступним повтором."
        ),
        "ui.status.replay.reopen_ticket": (
            "Повтор завершено, але стан економічного сеансу недоступний; виконайте відновлення робочої області."
        ),
        "ui.evaluation.reopen_failed": (
            "Оцінювання недоступне: повторне відкриття робочої області після повтору не пройшло закриту при помилці перевірку."
        ),
        "ui.error.replay.reopen": "Повторне відкриття робочої області після повтору відхилено закрито при помилці: {detail}",
        "ui.status.replay.reopen_blocked": (
            "Завершальний стан повтору не можна безпечно підтвердити; цю економічну робочу область заблоковано "
            "закрито при помилці. Виконайте «Відновити робочу область» або Control+Shift+R перед наступним повтором у цій робочій області."
        ),
        "ui.status.tickets.startup_failure": (
            "Економічний стан недоступний через помилку перевірки запуску; поточний знімок лише для читання "
            "лишається доступним, а паперовий повтор потребує відновлення."
        ),
        "ui.status.tickets.replay_running": "Повтор виконується; стан квитків оновиться після завершення транзакції.",
        "ui.status.close.replay_busy": (
            "Паперовий повтор ще виконується. Закриття програми заблоковано до завершення економічної транзакційної "
            "межі, щоб процес не обірвав фіксацію транзакції у довільній точці."
        ),
        "ui.status.close.live_busy": (
            "Поточний знімок ще виконується. Закриття програми заблоковано до завершального стану живого спостереження, "
            "щоб процес не приховав незавершену межу збереження ринку/стану джерела."
        ),
        "ui.status.close.recovery_busy": (
            "Відновлення робочої області ще виконується. Закриття програми заблоковано до завершального стану "
            "відновлення, щоб процес не обірвав економічне узгодження у довільній точці."
        ),
        "ui.accessibility.strategy.name": "Стратегія повтору",
        "ui.accessibility.strategy.description": (
            "Канонічна стратегія для вибору. Для типізованого дослідницького повтору потрібен план дослідження."
        ),
        "ui.accessibility.research_plan.name": "Вибрати план дослідження",
        "ui.accessibility.research_plan.description": (
            "Вибирає та перевіряє типізований причинний JSON-план дослідження для research-replay-v1."
        ),
        "ui.accessibility.choose_dataset.name": "Вибрати набір даних для повтору",
        "ui.accessibility.choose_dataset.description": (
            "Відкриває вибір папки набору даних для повтору і перевіряє її у фоновому процесі лише для читання. "
            "Гаряча клавіша Control+O."
        ),
        "ui.accessibility.run_replay.name": "Запустити паперовий повтор",
        "ui.accessibility.run_replay.description": (
            "Запускає причинний паперовий повтор для вибраного набору даних і канонічної стратегії. Гаряча клавіша Control+R."
        ),
        "ui.accessibility.repair_workspace.name": "Відновити робочу область",
        "ui.accessibility.repair_workspace.description": (
            "Запускає закрите при помилці відновлення робочої області для вибраної канонічної стратегії. "
            "Гаряча клавіша Control+Shift+R."
        ),
        "ui.accessibility.replay_speed.name": "Швидкість повтору",
        "ui.accessibility.replay_speed.description": "Вибір подієвого, 1×, 10×, 100× або 1000× режиму повтору.",
        "ui.accessibility.live_mode.name": "Режим живого спостереження",
        "ui.accessibility.live_mode.description": "Публічний перегляд без ключа або автентифікований ключ API із середовища.",
        "ui.accessibility.live_refresh.name": "Оновити поточний знімок",
        "ui.accessibility.live_refresh.description": (
            "Запускає один знімок настільного тенісу лише для читання у фоновому процесі. Гаряча клавіша Control+L."
        ),
        "ui.accessibility.live_quotes.name": "Поточні котирування",
        "ui.accessibility.live_quotes.description": "Поточні котирування лише для читання з останнього знімка. F7 переводить сюди фокус.",
        "ui.accessibility.tickets.name": "Паперові квитки і результати",
        "ui.accessibility.tickets.description": "Список віртуальних квитків та їх поточних результатів. F6 переводить сюди фокус.",
        "ui.accessibility.evaluation.name": "Оцінювання та докази портфеля",
        "ui.accessibility.evaluation.description": (
            "Підсумок останнього завершеного паперового повтору: банк, ROI, результати квитків і явно позначені "
            "сценарії портфеля. F8 переводить сюди фокус."
        ),
        "ui.accessibility.log.name": "Журнал виконання",
        "ui.accessibility.log.description": "Текстовий журнал повтору, спостереження, розрахунку результатів та оцінювання.",
        "ui.accessibility.bankroll.name": "Віртуальний банк",
        "ui.accessibility.bankroll.description": (
            "Поле лише для читання з поточним віртуальним банком, зарезервованою паперовою ставкою, "
            "канонічною стратегією та робочою областю. Доступне переходом Tab."
        ),
        "ui.result.summary": (
            "Повтор {run_id}: подій={event_count}; баланс={balance}; чистий_результат={net_profit}; "
            "завершено={settled}; портфель={portfolio_mode}; найгірше={worst}; найкраще={best}."
        ),
        "ui.price_truth.error.missing": "Істина ціни | ПОМИЛКА — доказ підсумку запуску відсутній або не читається.",
        "ui.price_truth.error.depth": "Істина ціни | ПОМИЛКА — вкладеність JSON підсумку запуску надто глибока.",
        "ui.price_truth.error.json": "Істина ціни | ПОМИЛКА — JSON підсумку запуску некоректний: {detail}.",
        "ui.price_truth.error.ambiguous": "Істина ціни | ПОМИЛКА — JSON підсумку запуску неоднозначний або неканонічний: {detail}.",
        "ui.price_truth.error.root": "Істина ціни | ПОМИЛКА — корінь підсумку запуску не є об’єктом.",
        "ui.price_truth.error.explicit": "Істина ціни | ПОМИЛКА — некоректна явно зафіксована істина запуску: {detail}.",
        "ui.price_truth.betfair_last_traded": (
            "Істина ціни | Спостереження Betfair last-traded/last-matched; виконувану котировку перевірено={executable}; "
            "відповідність паперового виконання перевірено={fill_fidelity}."
        ),
        "ui.price_truth.generic": (
            "Істина ціни | {price_semantics}; виконувану котировку перевірено={executable}; відповідність паперового "
            "виконання перевірено={fill_fidelity}."
        ),
        "ui.portfolio.mode.exact": "точний — усі релевантні сценарії цього звіту портфеля перебрано",
        "ui.portfolio.mode.approximate": "наближений — сценарії вибіркові; гарантії найгіршого/найкращого не заявляються",
        "ui.evaluation.replay": "Повтор {run_id} | події {event_count} | завершені квитки {settled_count}",
        "ui.evaluation.bankroll": (
            "Банк | початковий {initial_bankroll} | кінцевий {final_balance} | зарезервовано {committed_stake} | "
            "завершена сума ставок {settled_stake}"
        ),
        "ui.evaluation.metrics": "Оцінювання | чистий результат {net_profit} | ROI {roi} | виграно {won} | програно {lost} | повернено {void}",
        "ui.evaluation.portfolio": (
            "Портфель | {mode_truth} | сценарії {scenario_count} | найгірше {worst} | найкраще {best} | середнє {mean}"
        ),
        "ui.evaluation.truth": "Істина | лише паперова симуляція; це оцінювання не є доказом майбутньої прибутковості.",
        "ui.ticket.row": "{status} | ставка {stake} | коефіцієнт {odds} | виплата {payout} | {legs}",
        "ui.ticket.empty": "Паперові квитки ще відсутні.",
        "ui.observation.no_flags": "немає",
        "ui.observation.summary": (
            "Поточний знімок: джерело={source_id}; стан={health}; отримано={received}; прийнято={accepted}; "
            "відхилено={rejected}; поточних={current}; прапорці якості={quality_flags}."
        ),
        "ui.observation.quote": (
            "{event_id} | {market_type} | {market_id} | {selection_id} | коефіцієнт {odds} | час джерела {source_time}"
        ),
        "ui.observation.unknown_time": "невідомий",
        "ui.observation.empty": "Поточні котирування ще відсутні.",
    }
)

_CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType({DEFAULT_LOCALE: _UK_UA})


def catalog(locale: str = DEFAULT_LOCALE) -> Mapping[str, str]:
    """Return an immutable presentation catalog for an explicit supported locale."""

    try:
        return _CATALOGS[locale]
    except KeyError as exc:
        raise ValueError(f"unsupported locale: {locale!r}") from exc


def text(key: str, *, locale: str = DEFAULT_LOCALE, **values: object) -> str:
    """Render one presentation message without silently falling back to another locale."""

    messages = catalog(locale)
    try:
        template = messages[key]
    except KeyError as exc:
        raise KeyError(f"missing localization key {key!r} for locale {locale!r}") from exc

    render_values = values
    plan_identity = values.get("plan_identity")
    legacy_prefix = "; plan="
    if isinstance(plan_identity, str) and plan_identity.startswith(legacy_prefix):
        try:
            suffix_template = messages["ui.status.research_plan.identity_suffix"]
        except KeyError as exc:
            raise KeyError(
                f"missing localization key 'ui.status.research_plan.identity_suffix' for locale {locale!r}"
            ) from exc
        render_values = dict(values)
        render_values["plan_identity"] = suffix_template.format_map(
            {"sha_suffix": plan_identity[len(legacy_prefix):]}
        )

    try:
        return template.format_map(render_values)
    except KeyError as exc:
        missing = exc.args[0]
        raise KeyError(f"missing localization value {missing!r} for key {key!r}") from exc


def require_keys(keys: set[str] | frozenset[str], *, locale: str = DEFAULT_LOCALE) -> None:
    """Fail closed when a critical presentation surface lacks a locale entry."""

    missing = sorted(set(keys) - set(catalog(locale)))
    if missing:
        raise KeyError(f"missing localization keys for {locale!r}: {', '.join(missing)}")
