from __future__ import annotations

from autosport.localization import text


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
