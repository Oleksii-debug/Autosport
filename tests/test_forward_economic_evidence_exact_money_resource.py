from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.forward_economic_evidence import (
    ForwardEconomicEvidenceError,
    _exact_decimal_sum,
)


@pytest.mark.parametrize("large_value", ("1E+6000", "-1E+6000"))
def test_exact_money_extreme_exponent_gap_fails_closed_before_expansion(
    large_value: str,
) -> None:
    """Compact finite Decimals must not escape as raw bigint/string resource errors."""

    with pytest.raises(ForwardEconomicEvidenceError):
        _exact_decimal_sum(Decimal(large_value), Decimal("-0.1"))


def test_exact_money_resource_guard_preserves_small_loss_after_huge_gain() -> None:
    """The resource bound must retain the already-required exact-money regression."""

    result = _exact_decimal_sum(Decimal("1E+100"), Decimal("-1E-50"))
    expected = Decimal((0, (9,) * 150, -50))

    assert result == expected


@pytest.mark.parametrize("zero", ("0E-1000000", "-0E-1000000", "0E+1000000"))
def test_exact_money_zero_scale_does_not_create_artificial_exponent_gap(
    zero: str,
) -> None:
    assert _exact_decimal_sum(Decimal("1.25"), Decimal(zero)) == Decimal("1.25")


def test_exact_money_oversized_significand_fails_closed() -> None:
    oversized = Decimal("1" * 513)

    with pytest.raises(ForwardEconomicEvidenceError):
        _exact_decimal_sum(oversized, Decimal("0"))

