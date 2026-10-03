from __future__ import annotations

import re

from autosport.localization import catalog, text


_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_ALLOWED_MANUAL_TECHNICAL_TOKENS = frozenset(
    {
        "ID",
        "JSON",
        "Kelly",
        "decimal_odds",
        "false",
        "real_money_execution",
        "selection_id",
    }
)


def test_manual_calculation_generic_parameter_copy_is_ukrainian() -> None:
    assert text("ui.windows.manual_calculation.hint.fractional_kelly") == (
        "Введіть: ймовірність, коефіцієнт, частка, ліміт — по одному значенню в рядку."
    )
    assert text("ui.windows.manual_calculation.error.kelly") == (
        "Kelly потребує 4 значення: ймовірність, коефіцієнт, частка, ліміт."
    )
    assert text("ui.windows.manual_calculation.uia.calculate.description") == (
        "Запускає лише канонічний сервіс ручних розрахунків; реальні ставки не створюються."
    )

    for key in (
        "ui.windows.manual_calculation.hint.fractional_kelly",
        "ui.windows.manual_calculation.error.kelly",
        "ui.windows.manual_calculation.uia.calculate.description",
    ):
        rendered = text(key)
        assert "fraction" not in rendered.casefold()
        assert "cap" not in rendered.casefold()
        assert "manualcalculationservice" not in rendered.casefold()


def test_manual_calculation_catalog_has_no_unclassified_english_copy() -> None:
    manual_entries = {
        key: value
        for key, value in catalog().items()
        if key.startswith("ui.windows.manual_calculation.")
    }
    assert manual_entries

    for key, template in manual_entries.items():
        without_placeholders = re.sub(r"\{[^{}]+\}", "", template)
        tokens = set(_ASCII_TOKEN.findall(without_placeholders))
        unexpected = sorted(tokens - _ALLOWED_MANUAL_TECHNICAL_TOKENS)
        assert unexpected == [], (
            f"unclassified English presentation tokens for {key}: {unexpected!r} "
            f"in {template!r}"
        )
