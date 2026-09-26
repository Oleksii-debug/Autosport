from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from . import localization_v2 as _v2
from .localization_gui_evidence_export import GUI_EVIDENCE_EXPORT_UK_UA
from .localization_owner_economic import OWNER_ECONOMIC_AUTHORITY_UK_UA
from .localization_windows_replay_stop import WINDOWS_REPLAY_STOP_UK_UA
from .localization_windows_surfaces import WINDOWS_SURFACE_CONTENT_UK_UA


DEFAULT_LOCALE = _v2.DEFAULT_LOCALE
CATALOG_VERSION = 8

# Public v4 keeps one localization API while preserving the proven v2 catalog
# as an immutable base resource. Windows shell chrome and surface-contract
# presentation strings extend that same boundary rather than creating parallel
# UI text authorities.
_WINDOWS_SHELL_UK_UA = MappingProxyType(
    {
        "ui.windows.manual_calculation.frame.title": "Ручні розрахунки",
        "ui.windows.manual_calculation.button.open": "Відкрити ручні розрахунки",
        "ui.windows.manual_calculation.accessibility.open.name": "Відкрити робочу поверхню ручних розрахунків",
        "ui.windows.manual_calculation.accessibility.open.description": "Українська клавіатурна поверхня для ручних паперових/дослідницьких розрахунків без запису на диск і без реального виконання.",
        "ui.windows.manual_calculation.dialog.title": "Автоспорт — ручні розрахунки",
        "ui.windows.manual_calculation.dialog.description": "Введіть значення вручну. Формули, арифметика, класифікація і хеші належать канонічному сервісу; результат не записується на диск.",
        "ui.windows.manual_calculation.operation.label": "Операція:",
        "ui.windows.manual_calculation.input.label": "Вхідні значення:",
        "ui.windows.manual_calculation.calculate": "Обчислити",
        "ui.windows.manual_calculation.clear": "Очистити",
        "ui.windows.manual_calculation.close": "Закрити",
        "ui.windows.manual_calculation.status.ready": "Готово. Введіть значення для обраної операції.",
        "ui.windows.manual_calculation.status.success": "Готово: канонічний результат і evidence доступні лише для читання; real_money_execution=false.",
        "ui.windows.manual_calculation.status.error": "Розрахунок відхилено; частковий результат не показується.",
        "ui.windows.manual_calculation.status.cleared": "Очищено. Скасування/очищення нічого не записує.",
        "ui.windows.manual_calculation.result.heading": "Результат ручного розрахунку",
        "ui.windows.manual_calculation.result.operation": "Операція",
        "ui.windows.manual_calculation.result.method": "Метод (канонічний ID)",
        "ui.windows.manual_calculation.result.classification": "Статус точності",
        "ui.windows.manual_calculation.result.classification.exact": "точний",
        "ui.windows.manual_calculation.result.classification.approximate_decimal": "наближений десятковий",
        "ui.windows.manual_calculation.result.inputs": "Вхідні значення та одиниці",
        "ui.windows.manual_calculation.result.outputs": "Результати та одиниці",
        "ui.windows.manual_calculation.result.unit": "одиниця",
        "ui.windows.manual_calculation.result.assumptions": "Припущення",
        "ui.windows.manual_calculation.result.warnings": "Попередження",
        "ui.windows.manual_calculation.result.none": "немає",
        "ui.windows.manual_calculation.result.input_hash": "Хеш нормалізованого входу",
        "ui.windows.manual_calculation.result.result_hash": "Хеш результату",
        "ui.windows.manual_calculation.result.evidence_hash": "Хеш evidence",
        "ui.windows.manual_calculation.result.service_version": "Версія сервісу",
        "ui.windows.manual_calculation.result.input_mode": "Режим вводу",
        "ui.windows.manual_calculation.result.real_money_execution": "Реальне виконання",
        "ui.windows.manual_calculation.result.real_money_false": "ні (real_money_execution=false)",
        "ui.windows.manual_calculation.result.canonical_json": "Канонічний evidence JSON (незмінений)",
        "ui.windows.manual_calculation.error.unlocalized_evidence": "Канонічний текст evidence не має українського представлення: {value}",
        "ui.windows.manual_calculation.message.decimal_odds_supplied": "Десятковий коефіцієнт є наданим вхідним значенням аналізу.",
        "ui.windows.manual_calculation.message.american_unrounded": "Американський коефіцієнт показано як неокруглене математичне перетворення; правила відображення букмекера можуть його округляти.",
        "ui.windows.manual_calculation.message.american_decimal_context": "Американський коефіцієнт показано як неокруглене математичне перетворення у детермінованому десятковому контексті.",
        "ui.windows.manual_calculation.message.american_display_rounding": "Правила відображення букмекера можуть округляти американський коефіцієнт.",
        "ui.windows.manual_calculation.message.division_rounding": "Ділення округлюється в детермінованому десятковому контексті.",
        "ui.windows.manual_calculation.message.one_market": "Усі вибори належать до одного наданого ринку.",
        "ui.windows.manual_calculation.message.devig_model": "Мультиплікативна нормалізація є методом моделювання, а не об’єктивною справедливою вартістю.",
        "ui.windows.manual_calculation.message.probability_caller": "Ймовірність надана користувачем і не виводиться цим калькулятором.",
        "ui.windows.manual_calculation.message.paper_only": "Розрахунок лише паперовий; повноваження реального виконання відсутнє.",
        "ui.windows.manual_calculation.message.probability_research": "Ймовірність є дослідницьким припущенням, наданим користувачем.",
        "ui.windows.manual_calculation.message.kelly_single_position": "Формула Kelly застосовується до однієї позиції; залежність з іншими позиціями не моделюється.",
        "ui.windows.manual_calculation.message.paper_research": "Лише паперове дослідження; результат не є повноваженням на виконання.",
        "ui.windows.manual_calculation.message.kelly_rounding": "Ділення у формулі Kelly округлюється в детермінованому десятковому контексті.",
        "ui.windows.manual_calculation.message.balances_chronological": "Баланси надані у хронологічному порядку.",
        "ui.windows.manual_calculation.message.drawdown_rounding": "Ділення частки просадки округлюється в детермінованому десятковому контексті.",
        "ui.windows.manual_calculation.error.nonempty": "Потрібно ввести всі обов'язкові значення.",
        "ui.windows.manual_calculation.operation.odds_conversion": "Перетворення коефіцієнта",
        "ui.windows.manual_calculation.operation.implied_probability": "Ймовірність із коефіцієнта",
        "ui.windows.manual_calculation.operation.multiplicative_devig": "Мультиплікативне зняття маржі",
        "ui.windows.manual_calculation.operation.expected_return": "Очікуваний результат",
        "ui.windows.manual_calculation.operation.paper_payout": "Паперова виплата",
        "ui.windows.manual_calculation.operation.fractional_kelly": "Обмежений дробовий Kelly",
        "ui.windows.manual_calculation.operation.maximum_drawdown": "Максимальна просадка",
        "ui.windows.manual_calculation.hint.odds_conversion": "Введіть один скінченний десятковий коефіцієнт.",
        "ui.windows.manual_calculation.hint.implied_probability": "Введіть один скінченний десятковий коефіцієнт.",
        "ui.windows.manual_calculation.hint.multiplicative_devig": "Кожен рядок: selection_id=decimal_odds; ідентифікатори не повторюються.",
        "ui.windows.manual_calculation.hint.expected_return": "Введіть: ймовірність, десятковий коефіцієнт, ставка — по одному значенню в рядку.",
        "ui.windows.manual_calculation.hint.paper_payout": "Введіть: ставка, десятковий коефіцієнт — по одному значенню в рядку.",
        "ui.windows.manual_calculation.hint.fractional_kelly": "Введіть: ймовірність, коефіцієнт, fraction, cap — по одному значенню в рядку.",
        "ui.windows.manual_calculation.hint.maximum_drawdown": "Введіть послідовність балансів, по одному значенню в рядку.",
        "ui.windows.manual_calculation.uia.operation.name": "Операція ручного розрахунку",
        "ui.windows.manual_calculation.uia.operation.description": "Вибір канонічної ручної операції; формула залишається в сервісі.",
        "ui.windows.manual_calculation.uia.input.name": "Вхідні значення ручного розрахунку",
        "ui.windows.manual_calculation.uia.input.description": "Лише ручний текстовий ввід; помилкові та дубльовані значення відхиляються.",
        "ui.windows.manual_calculation.uia.calculate.name": "Обчислити ручний результат",
        "ui.windows.manual_calculation.uia.calculate.description": "Запускає лише канонічний ManualCalculationService; реальні ставки не створюються.",
        "ui.windows.manual_calculation.uia.result.name": "Результат і evidence ручного розрахунку",
        "ui.windows.manual_calculation.uia.result.description": "Лише для читання: канонічний результат, припущення, попередження та evidence hash.",
        "ui.windows.manual_calculation.uia.clear.name": "Очистити ручні значення",
        "ui.windows.manual_calculation.uia.clear.description": "Очищає локальні поля без запису на диск.",
        "ui.windows.manual_calculation.uia.close.name": "Закрити ручні розрахунки",
        "ui.windows.manual_calculation.uia.close.description": "Закриває робочу поверхню без запису результату.",
        "ui.windows.manual_calculation.error.duplicate": "Повторний ідентифікатор вибору: {selection}.",
        "ui.windows.manual_calculation.error.selection_format": "Для зняття маржі кожен рядок має бути у форматі selection_id=коефіцієнт.",
        "ui.windows.manual_calculation.error.selection_empty": "Ідентифікатор вибору та коефіцієнт не можуть бути порожніми.",
        "ui.windows.manual_calculation.error.single": "Ця операція приймає рівно одне числове значення.",
        "ui.windows.manual_calculation.error.expected_return": "Очікуваний результат потребує 3 значення: ймовірність, коефіцієнт, ставка.",
        "ui.windows.manual_calculation.error.paper_payout": "Паперова виплата потребує 2 значення: ставка, коефіцієнт.",
        "ui.windows.manual_calculation.error.kelly": "Kelly потребує 4 значення: ймовірність, коефіцієнт, fraction, cap.",
        "ui.windows.manual_calculation.error.unknown": "Невідома ручна операція.",
        "ui.windows.manual_calculation.error.operation_empty": "Операція не вибрана.",
        "ui.windows.shell.frame.title": "Навігація продукту",
        "ui.windows.shell.screen.label": "Екран:",
        "ui.windows.shell.button.open": "Перейти до робочої поверхні",
        "ui.windows.shell.state.active": "Активна робоча поверхня",
        "ui.windows.shell.state.disabled": "Вимкнено: {reason}",
        "ui.windows.shell.state.presentation": "Лише інформація — без доменної дії",
        "ui.windows.shell.accessibility.navigation.name": "Навігація екранами Автоспорт",
        "ui.windows.shell.accessibility.navigation.description": (
            "Виберіть один із канонічних екранів. F2 переводить фокус сюди; "
            "Control+Alt+Left/Right рухає між екранами."
        ),
        "ui.windows.shell.accessibility.state.name": "Стан вибраної поверхні",
        "ui.windows.shell.accessibility.state.description": (
            "Лише для читання: активна, інформаційна або видима, але вимкнена можливість."
        ),
        "ui.windows.shell.accessibility.open.name": "Перейти до робочої поверхні",
        "ui.windows.shell.accessibility.open.description": (
            "Переводить фокус до вже реалізованого робочого контролу. "
            "Для неактивних можливостей кнопка вимкнена."
        ),
        "ui.windows.shell.accessibility.details.name": "Контракт вибраного екрана",
        "ui.windows.shell.accessibility.details.description": (
            "Опис лише для читання: задача, клавіатура, стани, збереження та межі "
            "доменної істини вибраного екрана."
        ),
        "ui.windows.surface.phase.active": "СТАН: активна робоча поверхня",
        "ui.windows.surface.phase.disabled": (
            "СТАН: видима, але функція вимкнена до активації можливості"
        ),
        "ui.windows.surface.phase.presentation": "СТАН: лише інформаційна поверхня",
        "ui.windows.surface.detail.purpose": "Призначення",
        "ui.windows.surface.detail.primary_task": "Основна дія",
        "ui.windows.surface.detail.controls": "Елементи керування",
        "ui.windows.surface.detail.focus_entry": "Вхід фокусу",
        "ui.windows.surface.detail.focus_exit": "Вихід фокусу",
        "ui.windows.surface.detail.accessibility": "Доступність",
        "ui.windows.surface.detail.states": "Стани",
        "ui.windows.surface.detail.confirmation": "Підтвердження/скасування",
        "ui.windows.surface.detail.truth_boundary": "Межа істини",
        "ui.windows.surface.detail.restart": "Перезапуск",
        "ui.windows.surface.detail.blocked_reason": "Чому вимкнено",
    }
)

_OWNER_COLLISIONS = set(_WINDOWS_SHELL_UK_UA).intersection(OWNER_ECONOMIC_AUTHORITY_UK_UA)
if _OWNER_COLLISIONS:
    raise RuntimeError(f"localization v5 shell/owner resources collide: {sorted(_OWNER_COLLISIONS)!r}")

_CUSTOM_COLLISIONS = set(_WINDOWS_SHELL_UK_UA).intersection(WINDOWS_SURFACE_CONTENT_UK_UA)
if _CUSTOM_COLLISIONS:
    raise RuntimeError(f"localization v4 custom resources collide: {sorted(_CUSTOM_COLLISIONS)!r}")

_SURFACE_OWNER_COLLISIONS = set(WINDOWS_SURFACE_CONTENT_UK_UA).intersection(
    OWNER_ECONOMIC_AUTHORITY_UK_UA
)
if _SURFACE_OWNER_COLLISIONS:
    raise RuntimeError(
        f"localization v5 surface/owner resources collide: {sorted(_SURFACE_OWNER_COLLISIONS)!r}"
    )

_GUI_EVIDENCE_COLLISIONS = set(GUI_EVIDENCE_EXPORT_UK_UA).intersection(
    set(_WINDOWS_SHELL_UK_UA)
    | set(WINDOWS_SURFACE_CONTENT_UK_UA)
    | set(OWNER_ECONOMIC_AUTHORITY_UK_UA)
)
if _GUI_EVIDENCE_COLLISIONS:
    raise RuntimeError(
        f"localization v7 GUI evidence resources collide: {sorted(_GUI_EVIDENCE_COLLISIONS)!r}"
    )

_REPLAY_STOP_COLLISIONS = set(WINDOWS_REPLAY_STOP_UK_UA).intersection(
    set(_WINDOWS_SHELL_UK_UA)
    | set(WINDOWS_SURFACE_CONTENT_UK_UA)
    | set(OWNER_ECONOMIC_AUTHORITY_UK_UA)
    | set(GUI_EVIDENCE_EXPORT_UK_UA)
)
if _REPLAY_STOP_COLLISIONS:
    raise RuntimeError(
        f"localization v8 replay STOP resources collide: {sorted(_REPLAY_STOP_COLLISIONS)!r}"
    )

_CUSTOM_UK_UA = MappingProxyType(
    {
        **_WINDOWS_SHELL_UK_UA,
        **WINDOWS_SURFACE_CONTENT_UK_UA,
        **OWNER_ECONOMIC_AUTHORITY_UK_UA,
        **GUI_EVIDENCE_EXPORT_UK_UA,
        **WINDOWS_REPLAY_STOP_UK_UA,
    }
)
_BASE_UK_UA = _v2.catalog(DEFAULT_LOCALE)
_COLLISIONS = set(_BASE_UK_UA).intersection(_CUSTOM_UK_UA)
if _COLLISIONS:
    raise RuntimeError(f"localization v8 duplicates v2 keys: {sorted(_COLLISIONS)!r}")

_UK_UA = MappingProxyType({**_BASE_UK_UA, **_CUSTOM_UK_UA})
_CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType({DEFAULT_LOCALE: _UK_UA})


def catalog(locale: str = DEFAULT_LOCALE) -> Mapping[str, str]:
    """Return the immutable public presentation catalog for a supported locale."""

    try:
        return _CATALOGS[locale]
    except KeyError as exc:
        raise ValueError(f"unsupported locale: {locale!r}") from exc


def text(key: str, *, locale: str = DEFAULT_LOCALE, **values: object) -> str:
    """Render one public presentation message with no silent locale/key fallback."""

    if key not in _CUSTOM_UK_UA:
        return _v2.text(key, locale=locale, **values)

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
