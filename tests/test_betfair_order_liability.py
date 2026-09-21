from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from autosport.betfair_order_liability import (
    BetfairBetTargetType,
    BetfairMarketBettingType,
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
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=side,
        order_type=BetfairOrderType.LIMIT,
        price=D(price),
        size=D(size),
    )
    assert result.reserve == D(expected)
    assert result.raw_reserve == Fraction(D(expected))
    assert result.execution_authority is False


@pytest.mark.parametrize(
    ("side", "target_type", "price", "target", "raw"),
    [
        (
            BetfairOrderSide.BACK,
            BetfairBetTargetType.PAYOUT,
            "10",
            "10",
            Fraction(1),
        ),
        (
            BetfairOrderSide.BACK,
            BetfairBetTargetType.BACKERS_PROFIT,
            "6",
            "10",
            Fraction(2),
        ),
        (
            BetfairOrderSide.LAY,
            BetfairBetTargetType.PAYOUT,
            "10",
            "10",
            Fraction(9),
        ),
        (
            BetfairOrderSide.LAY,
            BetfairBetTargetType.BACKERS_PROFIT,
            "10",
            "10",
            Fraction(10),
        ),
    ],
)
def test_target_mode_exact_formula(side, target_type, price, target, raw):
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=side,
        order_type=BetfairOrderType.LIMIT,
        price=D(price),
        target_type=target_type,
        target_size=D(target),
        currency_quantum=D("0.01"),
    )
    assert result.raw_reserve == raw
    assert Fraction(result.reserve) >= raw
    assert result.execution_authority is False


def test_target_mode_repeating_ratio_is_exact_before_rounding():
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=BetfairOrderSide.BACK,
        order_type=BetfairOrderType.LIMIT,
        price=D("3"),
        target_type=BetfairBetTargetType.PAYOUT,
        target_size=D("1"),
        currency_quantum=D("0.01"),
    )
    assert result.raw_reserve == Fraction(1, 3)
    assert result.backer_stake_equivalent == Fraction(1, 3)
    assert result.reserve == D("0.34")


def test_target_mode_non_cent_quantum_rounds_up():
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=BetfairOrderSide.LAY,
        order_type=BetfairOrderType.LIMIT,
        price=D("2.7"),
        target_type=BetfairBetTargetType.PAYOUT,
        target_size=D("1"),
        currency_quantum=D("0.05"),
    )
    assert Fraction(result.reserve) >= result.raw_reserve
    assert result.reserve % D("0.05") == 0


def test_standard_lay_reserve_is_independent_of_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 4
        result = derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.LAY,
            order_type=BetfairOrderType.LIMIT,
            price=D("123.4567"),
            size=D("98.7654"),
        )
    assert Fraction(result.reserve) == result.raw_reserve
    assert result.reserve == D("12094.48495818")


def test_target_reserve_is_independent_of_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 4
        result = derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("3"),
            target_type=BetfairBetTargetType.PAYOUT,
            target_size=D("1"),
            currency_quantum=D("0.01"),
        )
    assert result.raw_reserve == Fraction(1, 3)
    assert result.reserve == D("0.34")


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
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=side,
        order_type=order_type,
        liability=D("12.34"),
        **kwargs,
    )
    assert result.reserve == D("12.34")
    assert result.raw_reserve == Fraction(D("12.34"))
    assert result.execution_authority is False


def test_each_way_back_standard_limit_doubles_reserve():
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=BetfairOrderSide.BACK,
        order_type=BetfairOrderType.LIMIT,
        price=D("5"),
        size=D("10"),
        each_way=True,
    )
    assert result.reserve == D("20")
    assert result.raw_reserve == Fraction(20)
    assert result.backer_stake_equivalent == Fraction(10)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.LAY,
            order_type=BetfairOrderType.LIMIT,
            price=D("5"),
            size=D("10"),
            each_way=True,
        ),
        dict(
            market_betting_type=BetfairMarketBettingType.ODDS,
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
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=bad,
            size=D("1"),
        )


@pytest.mark.parametrize("bad", [D("0"), D("-1"), D("NaN"), D("Infinity")])
def test_nonpositive_or_nonfinite_size_fails_closed(bad):
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            size=bad,
        )


def test_limit_price_must_exceed_one():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.LAY,
            order_type=BetfairOrderType.LIMIT,
            price=D("1"),
            size=D("10"),
        )


def test_target_requires_quantum():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            target_type=BetfairBetTargetType.PAYOUT,
            target_size=D("10"),
        )


def test_target_rejects_simultaneous_standard_size():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.ODDS,
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
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.MARKET_ON_CLOSE,
            price=D("2"),
            liability=D("10"),
        )


def test_close_order_rejects_standard_size():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT_ON_CLOSE,
            price=D("2"),
            size=D("10"),
            liability=D("10"),
        )


def test_limit_rejects_liability_field():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            liability=D("10"),
        )


def test_reserve_witness_binds_exact_standard_inputs_against_rebinding():
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=BetfairOrderSide.LAY,
        order_type=BetfairOrderType.LIMIT,
        price=D("3"),
        size=D("10"),
    )
    assert result.price == D("3")
    assert result.size == D("10")
    assert result.liability is None
    assert result.currency_quantum is None

    mutations = (
        {"reserve": D("10")},
        {"raw_reserve": Fraction(10)},
        {"backer_stake_equivalent": Fraction(999)},
        {"backer_stake_equivalent": 10},
        {"side": BetfairOrderSide.BACK},
        {"price": D("2")},
    )
    for mutation in mutations:
        with pytest.raises(BetfairOrderLiabilityError):
            replace(result, **mutation)


def test_target_witness_binds_rounding_quantum_and_target_semantics():
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=BetfairOrderSide.BACK,
        order_type=BetfairOrderType.LIMIT,
        price=D("3"),
        target_type=BetfairBetTargetType.PAYOUT,
        target_size=D("1"),
        currency_quantum=D("0.01"),
    )
    assert result.price == D("3")
    assert result.size is None
    assert result.target_size == D("1")
    assert result.currency_quantum == D("0.01")

    with pytest.raises(BetfairOrderLiabilityError):
        replace(result, currency_quantum=D("0.10"))
    with pytest.raises(BetfairOrderLiabilityError):
        replace(result, target_type=BetfairBetTargetType.BACKERS_PROFIT)


def test_each_way_witness_cannot_be_rebound_to_lay():
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=BetfairOrderSide.BACK,
        order_type=BetfairOrderType.LIMIT,
        price=D("5"),
        size=D("10"),
        each_way=True,
    )
    with pytest.raises(BetfairOrderLiabilityError):
        replace(result, side=BetfairOrderSide.LAY)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            size=D("1"),
            currency_quantum=D("0.01"),
        ),
        dict(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.MARKET_ON_CLOSE,
            liability=D("1"),
            currency_quantum=D("0.01"),
        ),
        dict(
            market_betting_type=BetfairMarketBettingType.ODDS,
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT_ON_CLOSE,
            price=D("2"),
            liability=D("1"),
            currency_quantum=D("0.01"),
        ),
    ],
)
def test_non_target_modes_reject_semantically_ignored_currency_quantum(kwargs):
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(**kwargs)


def test_close_order_witness_binds_exact_liability_input():
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=BetfairOrderSide.BACK,
        order_type=BetfairOrderType.LIMIT_ON_CLOSE,
        price=D("2"),
        liability=D("12.34"),
    )
    assert result.price == D("2")
    assert result.liability == D("12.34")
    assert result.size is None
    assert result.currency_quantum is None
    with pytest.raises(BetfairOrderLiabilityError):
        replace(result, liability=D("1"))


@pytest.mark.parametrize(
    "market_betting_type",
    [
        BetfairMarketBettingType.LINE,
        BetfairMarketBettingType.RANGE,
        BetfairMarketBettingType.ASIAN_HANDICAP_DOUBLE_LINE,
        BetfairMarketBettingType.ASIAN_HANDICAP_SINGLE_LINE,
        BetfairMarketBettingType.FIXED_ODDS,
    ],
)
def test_non_odds_market_betting_semantics_fail_closed(market_betting_type):
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type=market_betting_type,
            side=BetfairOrderSide.LAY,
            order_type=BetfairOrderType.LIMIT,
            price=D("50"),
            size=D("10"),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            order_type=BetfairOrderType.LIMIT,
            price=D("50"),
            target_type=BetfairBetTargetType.PAYOUT,
            target_size=D("10"),
            currency_quantum=D("0.01"),
        ),
        dict(
            order_type=BetfairOrderType.MARKET_ON_CLOSE,
            liability=D("10"),
        ),
        dict(
            order_type=BetfairOrderType.LIMIT_ON_CLOSE,
            price=D("50"),
            liability=D("10"),
        ),
    ],
)
def test_line_target_and_bsp_paths_fail_before_odds_arithmetic(kwargs):
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type=BetfairMarketBettingType.LINE,
            side=BetfairOrderSide.LAY,
            **kwargs,
        )


def test_reserve_witness_binds_market_betting_type():
    result = derive_betfair_order_reserve(
        market_betting_type=BetfairMarketBettingType.ODDS,
        side=BetfairOrderSide.LAY,
        order_type=BetfairOrderType.LIMIT,
        price=D("3"),
        size=D("10"),
    )
    assert result.market_betting_type is BetfairMarketBettingType.ODDS
    with pytest.raises(BetfairOrderLiabilityError):
        replace(
            result,
            market_betting_type=BetfairMarketBettingType.LINE,
        )


def test_market_betting_type_requires_exact_enum():
    with pytest.raises(BetfairOrderLiabilityError):
        derive_betfair_order_reserve(
            market_betting_type="ODDS",
            side=BetfairOrderSide.BACK,
            order_type=BetfairOrderType.LIMIT,
            price=D("2"),
            size=D("10"),
        )
