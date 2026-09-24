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
    assert lay_adverse == Decimal("1000.00")
    assert lay_movement is PriceMovement.HIGHER_ODDS


def test_favorable_prices_keep_signed_spread_but_zero_adverse_slippage() -> None:
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
    assert back_adverse == Decimal("0")
    assert lay_spread == Decimal("-500.00")
    assert lay_adverse == Decimal("0")


def test_equal_price_has_zero_adverse_slippage_for_both_sides() -> None:
    _delta, back_spread, back_adverse, _movement = _price_metrics(
        Decimal("2.0"),
        Decimal("2.0"),
        side="BACK",
    )
    _delta, lay_spread, lay_adverse, _movement = _price_metrics(
        Decimal("2.0"),
        Decimal("2.0"),
        side="LAY",
    )

    assert back_spread == Decimal("0")
    assert back_adverse == Decimal("0")
    assert lay_spread == Decimal("0")
    assert lay_adverse == Decimal("0")


def test_near_even_lay_slippage_tracks_liability_not_raw_odds_ratio() -> None:
    _delta, raw_spread, adverse, movement = _price_metrics(
        Decimal("1.01"),
        Decimal("1.02"),
        side="LAY",
    )

    assert raw_spread is not None
    assert raw_spread < Decimal("100")
    assert adverse == Decimal("10000")
    assert movement is PriceMovement.HIGHER_ODDS


def test_lay_odds_without_positive_liability_basis_fail_closed() -> None:
    with pytest.raises(EvaluationUniverseIntegrityError, match="greater than one"):
        _price_metrics(Decimal("1"), Decimal("1.01"), side="LAY")


def test_unknown_side_fails_closed_when_price_economics_are_observed() -> None:
    with pytest.raises(EvaluationUniverseIntegrityError, match="unsupported bet side"):
        _price_metrics(Decimal("2.0"), Decimal("2.1"), side="UNKNOWN")
