from dataclasses import fields
from decimal import Decimal

import pytest

from autosport.betfair_italy_bsp_admission import (
    BACK_BSP_MINIMUM_EUR,
    LAY_BSP_MINIMUM_LIABILITY_EUR,
    MAX_PRESELECTED_RETURN_EUR,
    BetfairItalyBspAdmissionError,
    BetfairItalyBspAdmissionStatus,
    BetfairItalyBspInstruction,
    BetfairItalyBspOrderType,
    BetfairItalyBspSide,
    assess_betfair_italy_bsp_admission,
)


def instruction(*, order_type, side, liability, price=None):
    return BetfairItalyBspInstruction(
        order_type=order_type,
        side=side,
        liability_eur=liability,
        price_limit=price,
    )


def test_back_bsp_minimum_boundary_is_exact_and_non_authorizing():
    rejected = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=Decimal("1.99"),
        )
    )
    accepted_minimum = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=Decimal("2.00"),
        )
    )

    assert BACK_BSP_MINIMUM_EUR == Decimal("2")
    assert rejected.status is BetfairItalyBspAdmissionStatus.REJECTED
    assert rejected.reason == "below_bsp_minimum"
    assert accepted_minimum.status is BetfairItalyBspAdmissionStatus.UNKNOWN
    assert accepted_minimum.execution_authority is False


def test_lay_bsp_minimum_boundary_is_exact_and_non_authorizing():
    rejected = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side=BetfairItalyBspSide.LAY,
            liability=Decimal("9.99"),
        )
    )
    accepted_minimum = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side=BetfairItalyBspSide.LAY,
            liability=Decimal("10.00"),
        )
    )

    assert LAY_BSP_MINIMUM_LIABILITY_EUR == Decimal("10")
    assert rejected.status is BetfairItalyBspAdmissionStatus.REJECTED
    assert accepted_minimum.status is BetfairItalyBspAdmissionStatus.UNKNOWN
    assert accepted_minimum.execution_authority is False


def test_market_on_close_is_unknown_after_minimum_because_cap_is_not_precomputable():
    result = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=Decimal("5"),
        )
    )

    assert result.status is BetfairItalyBspAdmissionStatus.UNKNOWN
    assert (
        result.reason
        == "moc_eur10000_cap_not_precomputable_without_preselected_odds"
    )
    assert result.preselected_return_eur is None
    assert result.execution_authority is False


def test_back_limit_on_close_uses_stake_times_preselected_price():
    equality = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.LIMIT_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=Decimal("2"),
            price=Decimal("5000"),
        )
    )
    overflow = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.LIMIT_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=Decimal("2"),
            price=Decimal("5000.01"),
        )
    )

    assert MAX_PRESELECTED_RETURN_EUR == Decimal("10000")
    assert equality.preselected_return_eur == Decimal("10000")
    assert equality.status is BetfairItalyBspAdmissionStatus.ADMISSIBLE
    assert overflow.preselected_return_eur == Decimal("10000.02")
    assert overflow.status is BetfairItalyBspAdmissionStatus.REJECTED
    assert overflow.reason == "preselected_return_exceeds_eur10000"


def test_lay_limit_on_close_adds_reserved_liability_and_profit():
    equality = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.LIMIT_ON_CLOSE,
            side=BetfairItalyBspSide.LAY,
            liability=Decimal("5000"),
            price=Decimal("2"),
        )
    )
    overflow = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.LIMIT_ON_CLOSE,
            side=BetfairItalyBspSide.LAY,
            liability=Decimal("5000.01"),
            price=Decimal("2"),
        )
    )

    assert equality.preselected_return_eur == Decimal("10000")
    assert equality.status is BetfairItalyBspAdmissionStatus.ADMISSIBLE
    assert overflow.preselected_return_eur == Decimal("10000.02")
    assert overflow.status is BetfairItalyBspAdmissionStatus.REJECTED


def test_lay_limit_on_close_formula_is_exact_decimal_not_float_math():
    result = assess_betfair_italy_bsp_admission(
        instruction(
            order_type=BetfairItalyBspOrderType.LIMIT_ON_CLOSE,
            side=BetfairItalyBspSide.LAY,
            liability=Decimal("10"),
            price=Decimal("1.01"),
        )
    )

    assert result.preselected_return_eur == Decimal("1010")
    assert result.status is BetfairItalyBspAdmissionStatus.ADMISSIBLE


@pytest.mark.parametrize(
    "bad",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1")],
)
def test_nonfinite_and_nonpositive_liability_fail_closed(bad):
    with pytest.raises(BetfairItalyBspAdmissionError, match="liability_eur"):
        instruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=bad,
        )


@pytest.mark.parametrize("bad", [2, 2.0, "2", True])
def test_non_decimal_liability_contamination_fails_closed(bad):
    with pytest.raises(BetfairItalyBspAdmissionError, match="exact Decimal"):
        instruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=bad,
        )


@pytest.mark.parametrize(
    "bad",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("1"), Decimal("0.99")],
)
def test_invalid_limit_price_fails_closed(bad):
    with pytest.raises(BetfairItalyBspAdmissionError, match="price_limit"):
        instruction(
            order_type=BetfairItalyBspOrderType.LIMIT_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=Decimal("2"),
            price=bad,
        )


def test_order_type_price_shape_fails_closed():
    with pytest.raises(BetfairItalyBspAdmissionError, match="must not carry"):
        instruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=Decimal("2"),
            price=Decimal("2"),
        )

    with pytest.raises(BetfairItalyBspAdmissionError, match="requires price_limit"):
        instruction(
            order_type=BetfairItalyBspOrderType.LIMIT_ON_CLOSE,
            side=BetfairItalyBspSide.BACK,
            liability=Decimal("2"),
        )


def test_string_enums_and_forged_execution_authority_are_not_accepted():
    with pytest.raises(BetfairItalyBspAdmissionError, match="order_type"):
        BetfairItalyBspInstruction(
            order_type="MARKET_ON_CLOSE",
            side=BetfairItalyBspSide.BACK,
            liability_eur=Decimal("2"),
        )

    with pytest.raises(BetfairItalyBspAdmissionError, match="side"):
        BetfairItalyBspInstruction(
            order_type=BetfairItalyBspOrderType.MARKET_ON_CLOSE,
            side="BACK",
            liability_eur=Decimal("2"),
        )

    field_names = {item.name for item in fields(BetfairItalyBspInstruction)}
    assert "execution_authority" not in field_names


def test_assessor_rejects_noncanonical_instruction_object():
    with pytest.raises(BetfairItalyBspAdmissionError, match="instruction"):
        assess_betfair_italy_bsp_admission(object())
