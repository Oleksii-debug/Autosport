from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


DEFAULT_LOCALE = "uk-UA"
CATALOG_VERSION = 1

_UK_UA = MappingProxyType(
    {
        "ui.boolean.true": "так",
        "ui.boolean.false": "ні",
        "ui.app.title": "Автоспорт — V1 лабораторія паперового моделювання для Windows",
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
        "ui.strategy.display": "{strategy_id}",
        "ui.status.startup.ready": (
            "Готово. Виберіть папку набору даних для повтору або оновіть поточний знімок."
        ),
        "ui.status.startup.recovery_required": (
            "Економічний сеанс не пройшов перевірку запуску; паперовий повтор для базової "
            "робочої області заблоковано до відновлення. Поточний знімок лише для читання "
            "доступний через Control+L."
        ),
        "ui.status.dataset.none": "Набір даних не вибрано.",
        "ui.status.research_plan.baseline": (
            "План дослідження: для baseline-v1 не потрібен."
        ),
        "ui.status.live.never": "Поточний знімок ще не завантажувався.",
        "ui.status.live_quotes.empty": "Поточні котирування ще відсутні.",
        "ui.status.evaluation.empty": (
            "Оцінювання ще відсутнє. Запустіть паперовий повтор."
        ),
        "ui.accessibility.strategy.name": "Стратегія повтору",
        "ui.accessibility.strategy.description": (
            "Канонічна стратегія для вибору. Для типізованого дослідницького повтору потрібен "
            "план дослідження."
        ),
        "ui.accessibility.research_plan.name": "Вибрати план дослідження",
        "ui.accessibility.research_plan.description": (
            "Вибирає та перевіряє типізований причинний JSON-план дослідження для "
            "research-replay-v1."
        ),
        "ui.accessibility.choose_dataset.name": "Вибрати набір даних для повтору",
        "ui.accessibility.choose_dataset.description": (
            "Відкриває вибір папки набору даних для повтору і перевіряє її у фоновому "
            "процесі лише для читання. Гаряча клавіша Control+O."
        ),
        "ui.accessibility.run_replay.name": "Запустити паперовий повтор",
        "ui.accessibility.run_replay.description": (
            "Запускає причинний паперовий повтор для вибраного набору даних і канонічної "
            "стратегії. Гаряча клавіша Control+R."
        ),
        "ui.accessibility.repair_workspace.name": "Відновити робочу область",
        "ui.accessibility.repair_workspace.description": (
            "Запускає закрите при помилці відновлення робочої області для вибраної канонічної "
            "стратегії. Гаряча клавіша Control+Shift+R."
        ),
        "ui.accessibility.replay_speed.name": "Швидкість повтору",
        "ui.accessibility.replay_speed.description": (
            "Вибір подієвого, 1×, 10×, 100× або 1000× режиму повтору."
        ),
        "ui.accessibility.live_mode.name": "Режим живого спостереження",
        "ui.accessibility.live_mode.description": (
            "Публічний перегляд без ключа або автентифікований ключ API із середовища."
        ),
        "ui.accessibility.live_refresh.name": "Оновити поточний знімок",
        "ui.accessibility.live_refresh.description": (
            "Запускає один знімок настільного тенісу лише для читання у фоновому процесі. "
            "Гаряча клавіша Control+L."
        ),
        "ui.accessibility.live_quotes.name": "Поточні котирування",
        "ui.accessibility.live_quotes.description": (
            "Поточні котирування лише для читання з останнього знімка. F7 переводить сюди фокус."
        ),
        "ui.accessibility.tickets.name": "Паперові квитки і результати",
        "ui.accessibility.tickets.description": (
            "Список віртуальних квитків та їх поточних результатів. F6 переводить сюди фокус."
        ),
        "ui.accessibility.evaluation.name": "Оцінювання та докази портфеля",
        "ui.accessibility.evaluation.description": (
            "Підсумок останнього завершеного паперового повтору: банк, ROI, результати квитків "
            "і явно позначені сценарії портфеля. F8 переводить сюди фокус."
        ),
        "ui.accessibility.log.name": "Журнал виконання",
        "ui.accessibility.log.description": (
            "Текстовий журнал повтору, спостереження, розрахунку результатів та оцінювання."
        ),
        "ui.accessibility.bankroll.name": "Віртуальний банк",
        "ui.accessibility.bankroll.description": (
            "Поле лише для читання з поточним віртуальним банком, зарезервованою паперовою "
            "ставкою, канонічною стратегією та робочою областю. Доступне переходом Tab."
        ),
        "ui.result.summary": (
            "Повтор {run_id}: подій={event_count}; баланс={balance}; "
            "чистий_результат={net_profit}; завершено={settled}; "
            "портфель={portfolio_mode}; найгірше={worst}; найкраще={best}."
        ),
        "ui.price_truth.error.missing": (
            "Істина ціни | ПОМИЛКА — доказ підсумку запуску відсутній або не читається."
        ),
        "ui.price_truth.error.depth": (
            "Істина ціни | ПОМИЛКА — вкладеність JSON підсумку запуску надто глибока."
        ),
        "ui.price_truth.error.json": (
            "Істина ціни | ПОМИЛКА — JSON підсумку запуску некоректний: {detail}."
        ),
        "ui.price_truth.error.ambiguous": (
            "Істина ціни | ПОМИЛКА — JSON підсумку запуску неоднозначний або неканонічний: {detail}."
        ),
        "ui.price_truth.error.root": (
            "Істина ціни | ПОМИЛКА — корінь підсумку запуску не є об’єктом."
        ),
        "ui.price_truth.error.explicit": (
            "Істина ціни | ПОМИЛКА — некоректна явно зафіксована істина запуску: {detail}."
        ),
        "ui.price_truth.betfair_last_traded": (
            "Істина ціни | Спостереження Betfair last-traded/last-matched; "
            "виконувану котировку перевірено={executable}; "
            "відповідність паперового виконання перевірено={fill_fidelity}."
        ),
        "ui.price_truth.generic": (
            "Істина ціни | {price_semantics}; виконувану котировку перевірено={executable}; "
            "відповідність паперового виконання перевірено={fill_fidelity}."
        ),
        "ui.portfolio.mode.exact": (
            "точний — усі релевантні сценарії цього звіту портфеля перебрано"
        ),
        "ui.portfolio.mode.approximate": (
            "наближений — сценарії вибіркові; гарантії найгіршого/найкращого не заявляються"
        ),
        "ui.evaluation.replay": (
            "Повтор {run_id} | події {event_count} | завершені квитки {settled_count}"
        ),
        "ui.evaluation.bankroll": (
            "Банк | початковий {initial_bankroll} | кінцевий {final_balance} | "
            "зарезервовано {committed_stake} | завершена сума ставок {settled_stake}"
        ),
        "ui.evaluation.metrics": (
            "Оцінювання | чистий результат {net_profit} | ROI {roi} | "
            "виграно {won} | програно {lost} | повернено {void}"
        ),
        "ui.evaluation.portfolio": (
            "Портфель | {mode_truth} | сценарії {scenario_count} | "
            "найгірше {worst} | найкраще {best} | середнє {mean}"
        ),
        "ui.evaluation.truth": (
            "Істина | лише паперова симуляція; це оцінювання не є доказом майбутньої прибутковості."
        ),
        "ui.ticket.row": (
            "{status} | ставка {stake} | коефіцієнт {odds} | виплата {payout} | {legs}"
        ),
        "ui.ticket.empty": "Паперові квитки ще відсутні.",
        "ui.observation.no_flags": "немає",
        "ui.observation.summary": (
            "Поточний знімок: джерело={source_id}; стан={health}; отримано={received}; "
            "прийнято={accepted}; відхилено={rejected}; поточних={current}; "
            "прапорці якості={quality_flags}."
        ),
        "ui.observation.quote": (
            "{event_id} | {market_type} | {market_id} | {selection_id} | "
            "коефіцієнт {odds} | час джерела {source_time}"
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
    try:
        return template.format_map(values)
    except KeyError as exc:
        missing = exc.args[0]
        raise KeyError(f"missing localization value {missing!r} for key {key!r}") from exc


def require_keys(keys: set[str] | frozenset[str], *, locale: str = DEFAULT_LOCALE) -> None:
    """Fail closed when a critical presentation surface lacks a locale entry."""

    missing = sorted(set(keys) - set(catalog(locale)))
    if missing:
        raise KeyError(f"missing localization keys for {locale!r}: {', '.join(missing)}")
