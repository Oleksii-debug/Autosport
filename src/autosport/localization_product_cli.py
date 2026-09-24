from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


DEFAULT_LOCALE = "uk-UA"

PRODUCT_CLI_UK_UA = MappingProxyType(
    {
        "product.cli.description": (
            "Запустити канонічний довговічний PAPER-продукт Autosport. "
            "Облікові дані провайдера залишаються зовнішніми для Autosport і ніколи "
            "не передаються як аргументи командного рядка."
        ),
        "product.cli.help": "Показати цю довідку та завершити роботу.",
        "product.cli.usage_prefix": "використання: ",
        "product.cli.options_heading": "Параметри:",
        "product.cli.workspace.metavar": "ШЛЯХ",
        "product.cli.source_factory.metavar": "МОДУЛЬ:ФУНКЦІЯ",
        "product.cli.bankroll.metavar": "СУМА",
        "product.cli.max_cycles.metavar": "N",
        "product.cli.poll_seconds.metavar": "СЕКУНДИ",
        "product.cli.error.prefix": "помилка:",
        "product.cli.error.required": "Потрібні обов'язкові аргументи: {arguments}.",
        "product.cli.error.invalid_number": (
            "Некоректне числове значення для {option}: {value}."
        ),
        "product.cli.error.unrecognized": "Нерозпізнані аргументи: {arguments}.",
        "product.cli.error.missing_value": "Для параметра {option} потрібне одне значення.",
        "product.cli.error.generic": "Некоректні аргументи командного рядка.",
        "product.cli.workspace.help": (
            "Робочий каталог канонічного продукту. Якщо не вказано, використовується "
            "канонічний каталог даних користувача Autosport."
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


def catalog(locale: str = DEFAULT_LOCALE) -> Mapping[str, str]:
    """Expose the canonical catalog without creating a second text authority."""

    if locale != DEFAULT_LOCALE:
        raise ValueError(f"unsupported product CLI locale: {locale!r}")
    from .localization import catalog as canonical_catalog

    return canonical_catalog(locale)


def product_cli_text(
    key: str,
    *,
    locale: str = DEFAULT_LOCALE,
    **values: object,
) -> str:
    """Render a product-CLI key through the canonical localization facade."""

    if locale != DEFAULT_LOCALE:
        raise ValueError(f"unsupported product CLI locale: {locale!r}")
    if key not in PRODUCT_CLI_UK_UA:
        raise KeyError(
            f"missing product CLI localization key {key!r} for locale {locale!r}"
        )

    # Lazy import prevents the resource-extension import performed by localization.py
    # from forming a module-initialization cycle. Rendering still belongs exclusively
    # to the canonical localization facade/catalog.
    from .localization import text as canonical_text

    return canonical_text(key, locale=locale, **values)
