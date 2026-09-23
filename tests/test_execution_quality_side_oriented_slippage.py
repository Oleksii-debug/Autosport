from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.evaluation_universe import EvaluationUniverseIntegrityError
from autosport.execution_quality_evidence import PriceMovement, _price_metrics


def test_adverse_slippage_is_positive_for_bad_back_and_bad_lay_prices() -> None:
    back_delta, back_spread, back_adverse, back_movement = _price_metrics(
        Decimal("2.0"),
        Decimal("1.9"),
        side="BACK",
    )
    lay_delta, lay_spread, lay_adverse, lay_movement = _price_metrics(
        Decimal("2.0"),
        Decimal("2.1"),
        side="LAY",
    )

    assert back_delta == Decimal("-0.1")
    assert back_spread == Decimal("-500.00")
    assert back_adverse == Decimal("500.00")
    assert back_movement is PriceMovement.LOWER_ODDS

    assert lay_delta == Decimal("0.1")
    assert lay_spread == Decimal("500.00")
    assert lay_adverse == Decimal("500.00")
    assert lay_movement is PriceMovement.HIGHER_ODDS


def test_favorable_prices_are_negative_for_both_sides() -> None:
    _delta, back_spread, back_adverse, _movement = _price_metrics(
        Decimal("2.0"),
        Decimal("2.1"),
        side="BACK",
    )
    _delta, lay_spread, lay_adverse, _movement = _price_metrics(
        Decimal("2.0"),
        Decimal("1.9"),
        side="LAY",
    )

    assert back_spread == Decimal("500.00")
    assert back_adverse == Decimal("-500.00")
    assert lay_spread == Decimal("-500.00")
    assert lay_adverse == Decimal("-500.00")


def test_unknown_side_fails_closed_when_price_economics_are_observed() -> None:
    with pytest.raises(EvaluationUniverseIntegrityError, match="unsupported bet side"):
        _price_metrics(Decimal("2.0"), Decimal("2.1"), side="UNKNOWN")
