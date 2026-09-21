from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


DEFAULT_LOCALE = "uk-UA"

_PRODUCT_CLI_UK_UA = MappingProxyType(
    {
        "product.cli.description": (
            "Запустити канонічний довговічний PAPER-продукт Autosport. "
            "Облікові дані провайдера залишаються зовнішніми для Autosport і ніколи "
            "не передаються як аргументи командного рядка."
        ),
        "product.cli.help": "Показати цю довідку та завершити роботу.",
        "product.cli.usage_prefix": "використання: ",
        "product.cli.options_heading": "Параметри:",
        "product.cli.workspace.help": (
            "Робочий каталог канонічного продукту. За замовчуванням: .autosport-product."
        ),
        "product.cli.source_factory.help": (
            "Зовнішня фабрика джерела продукту у форматі module:function."
        ),
        "product.cli.bankroll.help": (
            "Початковий PAPER-банкрол; значення не надає повноважень реального виконання."
        ),
        "product.cli.max_cycles.help": (
            "Необов'язкова кількість циклів для обмеженого кваліфікаційного або "
            "контрольованого запуску."
        ),
        "product.cli.poll_seconds.help": (
            "Інтервал між циклами в секундах для безперервного запуску."
        ),
    }
)

_CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {DEFAULT_LOCALE: _PRODUCT_CLI_UK_UA}
)


def catalog(locale: str = DEFAULT_LOCALE) -> Mapping[str, str]:
    """Return immutable product-CLI presentation resources for one locale."""

    try:
        return _CATALOGS[locale]
    except KeyError as exc:
        raise ValueError(f"unsupported product CLI locale: {locale!r}") from exc


def product_cli_text(
    key: str,
    *,
    locale: str = DEFAULT_LOCALE,
    **values: object,
) -> str:
    """Render one product-CLI message without silent key or locale fallback."""

    messages = catalog(locale)
    try:
        template = messages[key]
    except KeyError as exc:
        raise KeyError(
            f"missing product CLI localization key {key!r} for locale {locale!r}"
        ) from exc
    try:
        return template.format_map(values)
    except KeyError as exc:
        missing = exc.args[0]
        raise KeyError(
            f"missing product CLI localization value {missing!r} for key {key!r}"
        ) from exc
