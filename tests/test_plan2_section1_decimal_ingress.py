"""Plan 2 Section 1: only exact decimal ingress may affect paper value economics."""
from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.probability import implied_probability, paper_value


class _HostileDecimalText(str):
    def __str__(self) -> str:
        raise AssertionError("caller-owned coercion must not run")


class _HostileDecimal(Decimal):
    def __str__(self) -> str:
        raise AssertionError("caller-owned coercion must not run")


@pytest.mark.parametrize("bad", [2.0, 1.01, True, 2, _HostileDecimalText("2")])
def test_implied_probability_rejects_noncanonical_odds_without_coercion(bad: object) -> None:
    with pytest.raises(ValueError, match="decimal odds must be a Decimal or decimal string"):
        implied_probability(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0.5, 1, False, _HostileDecimalText("0.5")])
def test_paper_value_rejects_noncanonical_probability_without_coercion(bad: object) -> None:
    with pytest.raises(ValueError, match="probability must be a Decimal or decimal string"):
        paper_value("quote", bad, Decimal("2"))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [2.0, 2, True, _HostileDecimal("2")])
def test_paper_value_rejects_noncanonical_odds_without_coercion(bad: object) -> None:
    with pytest.raises(ValueError, match="decimal odds must be a Decimal or decimal string"):
        paper_value("quote", Decimal("0.5"), bad)  # type: ignore[arg-type]


def test_accepted_decimal_values_remain_exact_and_side_effect_free() -> None:
    assert implied_probability(Decimal("2.5")) == Decimal("0.4")
    assert implied_probability("2.5") == Decimal("0.4")
    estimate = paper_value("quote", Decimal("0.55"), "2.5")
    assert estimate.probability == Decimal("0.55")
    assert estimate.decimal_odds == Decimal("2.5")
    assert estimate.expected_profit_per_unit == Decimal("0.375")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_transport_stays_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="decimal odds must be finite"):
        implied_probability(bad)
    with pytest.raises(ValueError, match="probability must be finite"):
        paper_value("quote", bad, "2")
