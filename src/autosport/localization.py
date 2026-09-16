from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


DEFAULT_LOCALE = "uk-UA"
CATALOG_VERSION = 1

_UK_UA = MappingProxyType(
    {
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
            "відповідність paper-fill перевірено={fill_fidelity}."
        ),
        "ui.price_truth.generic": (
            "Істина ціни | {price_semantics}; виконувану котировку перевірено={executable}; "
            "відповідність paper-fill перевірено={fill_fidelity}."
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
            "Live-знімок: джерело={source_id}; стан={health}; отримано={received}; "
            "прийнято={accepted}; відхилено={rejected}; поточних={current}; "
            "прапорці якості={quality_flags}."
        ),
        "ui.observation.quote": (
            "{event_id} | {market_type} | {market_id} | {selection_id} | "
            "коефіцієнт {odds} | час джерела {source_time}"
        ),
        "ui.observation.unknown_time": "невідомий",
        "ui.observation.empty": "Live-котирування ще відсутні.",
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
