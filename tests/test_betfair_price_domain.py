from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from autosport.betfair_order_liability import BetfairMarketBettingType
from autosport.betfair_price_domain import (
    BetfairPriceDomainError,
    BetfairPriceLadderType,
    BetfairStandardLimitPriceCheck,
    validate_betfair_standard_limit_price,
)


D = Decimal


@pytest.mark.parametrize(
    "price",
    [
        "1.01",
        "1.50",
        "2",
        "2.02",
        "3",
        "3.05",
        "4",
        "4.1",
        "6",
        "6.2",
        "10",
        "10.5",
        "20",
        "21",
        "30",
        "32",
        "50",
        "55",
        "100",
        "110",
        "1000",
    ],
)
def test_classic_valid_prices_are_accepted(price: str) -> None:
    result = validate_betfair_standard_limit_price(
        market_betting_type=BetfairMarketBettingType.ODDS,
        price_ladder_type=BetfairPriceLadderType.CLASSIC,
        price=D(price),
    )

    assert result.price == D(price)
    assert result.provider_increment_valid is True
    assert result.market_metadata_authority_proven is False
    assert result.execution_authority is False


@pytest.mark.parametrize(
    "price",
    [
        "1.00",
        "2.01",
        "3.01",
        "4.05",
        "6.1",
        "10.2",
        "20.5",
        "31",
        "52",
        "105",
        "1001",
    ],
)
def test_classic_off_ladder_prices_fail_closed(price: str) -> None:
    with pytest.raises(BetfairPriceDomainError):
        validate_betfair_standard_limit_price(
            market_betting_type=BetfairMarketBettingType.ODDS,
            price_ladder_type=BetfairPriceLadderType.CLASSIC,
            price=D(price),
        )


@pytest.mark.parametrize("price", ["1.01", "2.01", "3.99", "999.99", "1000"])
def test_finest_uses_exact_cent_increment(price: str) -> None:
    result = validate_betfair_standard_limit_price(
        market_betting_type=BetfairMarketBettingType.ODDS,
        price_ladder_type=BetfairPriceLadderType.FINEST,
        price=D(price),
    )
    assert result.provider_increment_valid is True


@pytest.mark.parametrize("price", ["1.005", "2.015", "1000.01"])
def test_finest_off_increment_or_range_fails_closed(price: str) -> None:
    with pytest.raises(BetfairPriceDomainError):
        validate_betfair_standard_limit_price(
            market_betting_type=BetfairMarketBettingType.ODDS,
            price_ladder_type=BetfairPriceLadderType.FINEST,
            price=D(price),
        )


@pytest.mark.parametrize(
    "bad",
    [2, 2.0, "2.00", True, None],
)
def test_price_requires_exact_decimal(bad: object) -> None:
    with pytest.raises(BetfairPriceDomainError, match="exact Decimal"):
        validate_betfair_standard_limit_price(
            market_betting_type=BetfairMarketBettingType.ODDS,
            price_ladder_type=BetfairPriceLadderType.CLASSIC,
            price=bad,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), D("-Infinity")])
def test_nonfinite_prices_fail_closed(bad: Decimal) -> None:
    with pytest.raises(BetfairPriceDomainError, match="finite"):
        validate_betfair_standard_limit_price(
            market_betting_type=BetfairMarketBettingType.ODDS,
            price_ladder_type=BetfairPriceLadderType.CLASSIC,
            price=bad,
        )


def test_line_range_requires_separate_market_line_range_authority() -> None:
    with pytest.raises(BetfairPriceDomainError, match="Market Line Range Info"):
        validate_betfair_standard_limit_price(
            market_betting_type=BetfairMarketBettingType.ODDS,
            price_ladder_type=BetfairPriceLadderType.LINE_RANGE,
            price=D("2"),
        )


@pytest.mark.parametrize(
    "market_type",
    [
        value
        for value in BetfairMarketBettingType
        if value is not BetfairMarketBettingType.ODDS
    ],
)
def test_non_odds_market_semantics_are_not_laundered_into_standard_limit(
    market_type: BetfairMarketBettingType,
) -> None:
    with pytest.raises(BetfairPriceDomainError, match="ODDS"):
        validate_betfair_standard_limit_price(
            market_betting_type=market_type,
            price_ladder_type=BetfairPriceLadderType.FINEST,
            price=D("2.01"),
        )


@pytest.mark.parametrize(
    ("market_type", "ladder_type"),
    [
        ("ODDS", BetfairPriceLadderType.CLASSIC),
        (BetfairMarketBettingType.ODDS, "CLASSIC"),
        (True, BetfairPriceLadderType.CLASSIC),
    ],
)
def test_enum_aliases_and_strings_fail_closed(
    market_type: object,
    ladder_type: object,
) -> None:
    with pytest.raises(BetfairPriceDomainError, match="must be exact"):
        validate_betfair_standard_limit_price(
            market_betting_type=market_type,  # type: ignore[arg-type]
            price_ladder_type=ladder_type,  # type: ignore[arg-type]
            price=D("2"),
        )


def test_decimal_context_cannot_change_classic_alignment() -> None:
    with localcontext() as context:
        context.prec = 2
        context.rounding = "ROUND_UP"

        valid = validate_betfair_standard_limit_price(
            market_betting_type=BetfairMarketBettingType.ODDS,
            price_ladder_type=BetfairPriceLadderType.CLASSIC,
            price=D("32"),
        )
        assert valid.provider_increment_valid is True

        with pytest.raises(BetfairPriceDomainError):
            validate_betfair_standard_limit_price(
                market_betting_type=BetfairMarketBettingType.ODDS,
                price_ladder_type=BetfairPriceLadderType.CLASSIC,
                price=D("31"),
            )


def test_direct_result_construction_is_still_self_validating() -> None:
    with pytest.raises(BetfairPriceDomainError):
        BetfairStandardLimitPriceCheck(
            market_betting_type=BetfairMarketBettingType.ODDS,
            price_ladder_type=BetfairPriceLadderType.CLASSIC,
            price=D("2.01"),
        )


def test_authority_flags_cannot_be_upgraded_by_dataclass_replace() -> None:
    result = validate_betfair_standard_limit_price(
        market_betting_type=BetfairMarketBettingType.ODDS,
        price_ladder_type=BetfairPriceLadderType.CLASSIC,
        price=D("2"),
    )

    with pytest.raises(ValueError):
        replace(result, execution_authority=True)
    with pytest.raises(ValueError):
        replace(result, market_metadata_authority_proven=True)


def test_classic_full_ladder_has_exactly_350_unique_valid_prices() -> None:
    prices: list[Decimal] = []
    bands = (
        ("1.01", "2", "0.01"),
        ("2.02", "3", "0.02"),
        ("3.05", "4", "0.05"),
        ("4.1", "6", "0.1"),
        ("6.2", "10", "0.2"),
        ("10.5", "20", "0.5"),
        ("21", "30", "1"),
        ("32", "50", "2"),
        ("55", "100", "5"),
        ("110", "1000", "10"),
    )
    for start_text, end_text, step_text in bands:
        value = D(start_text)
        end = D(end_text)
        step = D(step_text)
        while value <= end:
            prices.append(value)
            value += step

    assert len(prices) == 350
    assert len(set(prices)) == 350
    for price in prices:
        assert validate_betfair_standard_limit_price(
            market_betting_type=BetfairMarketBettingType.ODDS,
            price_ladder_type=BetfairPriceLadderType.CLASSIC,
            price=price,
        ).provider_increment_valid is True
