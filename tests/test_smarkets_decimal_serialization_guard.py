from __future__ import annotations

from decimal import Decimal, localcontext

from autosport.smarkets_execution_reconciliation import _decimal_text


def test_decimal_text_preserves_provider_derived_value_across_ambient_precision() -> None:
    with localcontext() as context:
        context.prec = 50
        value = Decimal(10000) / Decimal(3500)

    expected = "2.8571428571428571428571428571428571428571428571429"
    assert _decimal_text(value) == expected

    for precision in (2, 5, 9, 28, 50):
        with localcontext() as context:
            context.prec = precision
            assert _decimal_text(value) == expected


def test_decimal_text_keeps_compact_fixed_point_projection() -> None:
    assert _decimal_text(Decimal("10.5000")) == "10.5"
    assert _decimal_text(Decimal("0.000")) == "0"
    assert _decimal_text(Decimal("-0.000")) == "-0"
