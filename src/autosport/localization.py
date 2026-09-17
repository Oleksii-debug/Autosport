from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from . import localization_v2 as _v2


DEFAULT_LOCALE = _v2.DEFAULT_LOCALE
CATALOG_VERSION = 3

# Public v3 keeps one localization API while preserving the proven v2 catalog
# as an immutable base resource. New Windows shell presentation strings extend
# that same boundary rather than creating a parallel UI authority.
_WINDOWS_SHELL_UK_UA = MappingProxyType(
    {
        "ui.windows.shell.frame.title": "Навігація продукту",
        "ui.windows.shell.screen.label": "Екран:",
        "ui.windows.shell.button.open": "Перейти до робочої поверхні",
        "ui.windows.shell.state.active": "Активна V1-поверхня",
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
    }
)

_BASE_UK_UA = _v2.catalog(DEFAULT_LOCALE)
_COLLISIONS = set(_BASE_UK_UA).intersection(_WINDOWS_SHELL_UK_UA)
if _COLLISIONS:
    raise RuntimeError(f"localization v3 duplicates v2 keys: {sorted(_COLLISIONS)!r}")

_UK_UA = MappingProxyType({**_BASE_UK_UA, **_WINDOWS_SHELL_UK_UA})
_CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType({DEFAULT_LOCALE: _UK_UA})


def catalog(locale: str = DEFAULT_LOCALE) -> Mapping[str, str]:
    """Return the immutable public presentation catalog for a supported locale."""

    try:
        return _CATALOGS[locale]
    except KeyError as exc:
        raise ValueError(f"unsupported locale: {locale!r}") from exc


def text(key: str, *, locale: str = DEFAULT_LOCALE, **values: object) -> str:
    """Render one public v3 message with no silent locale or key fallback."""

    if key not in _WINDOWS_SHELL_UK_UA:
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
