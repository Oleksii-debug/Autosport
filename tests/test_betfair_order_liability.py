from decimal import Decimal

import pytest

from autosport.betfair_order_liability import (
    BetfairBetTargetType,
    BetfairOrderLiabilityError,
    BetfairOrderSide,
    BetfairOrderType,
    derive_betfair_order_reserve,
)


def D(value: str) -> Decimal:
    return Decimal(value)


@pytest.mark.parametrize(
    ("side", "price", "size", "expected"),
    [
        (BetfairOrderSide.BACK, "2", "10", "10"),
        (BetfairOrderSide.BACK, "1000", "0.01", "0.01"),
        (BetfairOrderSide.LAY, "2", "10", "10"),
        (BetfairOrderSide.LAY, "3", "10", "20"),
        (BetfairOrderSide.LAY, "32", "10", "310"),
    ],
)
def test_standard_limit_reserve(side, price, size, expected):
    result = derive_betfair_order_reserve(
        side=side,
        order_type=BetfairOrderType.LIMIT,
        price=D(price),
        size=D(size),
    )
    assert result.reserve == D(expected)
    assert result.unrounded_reserve == D(expected)
    assert result.execution_authority is False


@pytest.mark.parametrize(
    ("side", "target_type", "price", "target", "expected"),
    [
        (
            BetfairOrderSide.BACK,
            BetfairBetTargetType.PAYOUT,
            "10",
            "10",
            "1",
        ),
        (
            BetfairOrderSide.BACK,
            BetfairBetTargetType.BACKERS_PROFIT,
            "6",
            "10",
            "2",
        ),
        (
            BetfairOrderSide.LAY,
            BetfairBetTargetType.PAYOUT,
            "10",
            "10",
            "9",
        ),
        (
            BetfairOrderSide.LAY,
            BetfairBetTargetType.BACKERS_PROFIT,
            "10",
            "10",
            "10",
        ),
    ],
)
def test_target_mode_exact_formula(side, target_type, price, target, expected):
    result = derive_betfair_order_reserve(
        side=side,
        order_type=BetfairOrderType.LIMIT,
        price=D(price),
        target_type=target_type,
        target_size=D(target),
        currency_quantum=D("0.01"),
    )
    assert result.reserve == D(expected)
    assert result.execution_authority is False


def test_target_mode_reserve_rounds_up_not_down():
    result = derive_betfair_order_reserve(
        side=BetfairOrderSide.BACK,
        order_type=BetfairOrderType.LIMIT,
        price=D("3"),
        target_type=BetfairBetTargetType.PAYOUT,
        target_size=D("1"),
        currency_quantum=D("0.01"),
    )
    assert result.unrounded_reserve == D("1") / D("3")
    assert result.reserve == D("0.34")


def test_target_mode_non_cent_quantum_rounds_up():
    result = derive_betfair_order_reserve(
        side=BetfairOrderSide.LAY,
        order_type=BetfairOrderType.LIMIT,
        price=D("2.7"),
        target_type=BetfairBetTargetType.PAYOUT,
        target_size=D("1"),
        currency_quantum=D("0.05"),
    )
    assert result.reserve >= result.unrounded_reserve
    assert result.reserve % D("0.05") == 0


@pytest.mark.parametrize("order_type", [
    BetfairOrderType.MARKET_ON_CLOSE,
    BetfairOrderType.LIMIT_ON_CLOSE,
])
@pytest.mark.parametrize("side", [
    BetfairOrderSide.BACK,
    BetfairOrderSide.LAY,
])
def test_bsp_close_order_reserves_explicit_liability(order_type, side):
    kwargs = {"price": D("5")} if order_type is BetfairOrderType.LIMIT_ON_CLOSE else {}
    result = derive_betfair_order_reserve(
        side=side,
        order_type=order_type,
        liability=D("12.34"),
        **kwargs,
    )
    assert result.reserve == D("12.34")
    assert result.execution_authority is False


def test_each_way_back_standard_limit_doubles_reserve():
    result = derive_betfair_order_reserve(
        side=BetfairOrderSide.BACK,
        order_type=BetfairOrderType.LIMIT,
        price=D("5"),
        size=D("10"),
        each_way=True,
    )
    assert result.reserve == D("20")
    assert result.backer_stake_equivalent == D("10")


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            side=BetfairOrderSide.LAY,
            order_type=BetfairOrderType.LIMIT,
            price=D("5"),
            size=D("10"),
            each_way=True,
        ),
        dict(
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("5"),
            target_type=BetfairBetTargetType.PAYOUT,
            target_size=D("10"),
            currency_quantum=D("0.01"),
            each_way=True,
        ),
    ],
)
def test_each_way_unsupported_combinations_fail_closed(kwargs):
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(**kwargs)


@pytest.mark.parametrize("bad", [0.0, 2, "2", True, None])
def test_price_requires_exact_decimal(bad):
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=bad,
            size=D("1"),
        )


@pytest.mark.parametrize("bad", [D("0"), D("-1"), D("NaN"), D("Infinity")])
def test_nonpositive_or_nonfinite_size_fails_closed(bad):
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            size=bad,
        )


def test_limit_price_must_exceed_one():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            side=BetfairOrderSide.LAY,
            order_type=BetfairOrderType.LIMIT,
            price=D("1"),
            size=D("10"),
        )


def test_target_requires_quantum():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            target_type=BetfairBetTargetType.PAYOUT,
            target_size=D("10"),
        )


def test_target_rejects_simultaneous_standard_size():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            size=D("1"),
            target_type=BetfairBetTargetType.PAYOUT,
            target_size=D("10"),
            currency_quantum=D("0.01"),
        )


def test_market_on_close_rejects_price():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.MARKET_ON_CLOSE,
            price=D("2"),
            liability=D("10"),
        )


def test_close_order_rejects_standard_size():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT_ON_CLOSE,
            price=D("2"),
            size=D("10"),
            liability=D("10"),
        )


def test_limit_rejects_liability_field():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            liability=D("10"),
        )
