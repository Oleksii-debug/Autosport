"""Deterministic Betfair standard LIMIT price-domain validation.

This module validates a supplied provider price-ladder classification against
the documented Betfair increments. It intentionally does not prove where the
market metadata came from and does not grant execution authority.

The supervised execution path can consume this deterministic contract only
after a separate product-owned market-metadata authority binds the exact
market to its provider PriceLadderType.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from .betfair_order_liability import BetfairMarketBettingType


class BetfairPriceDomainError(ValueError):
    """The supplied market/price combination is outside the supported domain."""


class BetfairPriceLadderType(str, Enum):
    """Provider-native Betfair price-ladder classification."""

    CLASSIC = "CLASSIC"
    FINEST = "FINEST"
    LINE_RANGE = "LINE_RANGE"


_CLASSIC_BANDS: tuple[tuple[Decimal, Decimal, Decimal], ...] = (
    (Decimal("2"), Decimal("1.01"), Decimal("0.01")),
    (Decimal("3"), Decimal("2"), Decimal("0.02")),
    (Decimal("4"), Decimal("3"), Decimal("0.05")),
    (Decimal("6"), Decimal("4"), Decimal("0.1")),
    (Decimal("10"), Decimal("6"), Decimal("0.2")),
    (Decimal("20"), Decimal("10"), Decimal("0.5")),
    (Decimal("30"), Decimal("20"), Decimal("1")),
    (Decimal("50"), Decimal("30"), Decimal("2")),
    (Decimal("100"), Decimal("50"), Decimal("5")),
    (Decimal("1000"), Decimal("100"), Decimal("10")),
)


def _exact_enum(value: object, enum_type: type[Enum], name: str) -> Enum:
    if type(value) is not enum_type:
        raise BetfairPriceDomainError(
            f"{name} must be exact {enum_type.__name__}"
        )
    return value


def _price(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise BetfairPriceDomainError("price must be exact Decimal")
    if not value.is_finite():
        raise BetfairPriceDomainError("price must be finite")
    return value


def _aligned(
    price: Decimal,
    *,
    anchor: Decimal,
    increment: Decimal,
) -> bool:
    """Return exact alignment without ambient Decimal-context arithmetic."""

    offset = Fraction(price) - Fraction(anchor)
    if offset < 0:
        return False
    units = offset / Fraction(increment)
    return units.denominator == 1


def _validate_price_increment(
    *,
    price: Decimal,
    price_ladder_type: BetfairPriceLadderType,
) -> None:
    if price_ladder_type is BetfairPriceLadderType.LINE_RANGE:
        raise BetfairPriceDomainError(
            "LINE_RANGE requires separately bound Market Line Range Info"
        )

    if price < Decimal("1.01") or price > Decimal("1000"):
        raise BetfairPriceDomainError(
            "Betfair exchange price must be within 1.01..1000"
        )

    if price_ladder_type is BetfairPriceLadderType.FINEST:
        if not _aligned(
            price,
            anchor=Decimal("1.01"),
            increment=Decimal("0.01"),
        ):
            raise BetfairPriceDomainError(
                "price is not aligned to FINEST 0.01 increment"
            )
        return

    if price_ladder_type is not BetfairPriceLadderType.CLASSIC:
        raise BetfairPriceDomainError("unsupported Betfair price ladder")

    for upper, anchor, increment in _CLASSIC_BANDS:
        if price <= upper:
            if not _aligned(price, anchor=anchor, increment=increment):
                raise BetfairPriceDomainError(
                    "price is not aligned to CLASSIC increment"
                )
            return
    raise BetfairPriceDomainError("price is outside CLASSIC ladder")


@dataclass(frozen=True, slots=True)
class BetfairStandardLimitPriceCheck:
    """Self-validating deterministic check over supplied Betfair metadata.

    market_metadata_authority_proven is permanently false here because this
    module does not acquire MarketDescription or MarketDefinition itself. A
    caller-constructible matching enum therefore cannot be confused with
    product-owned provider metadata or with execution permission.
    """

    market_betting_type: BetfairMarketBettingType
    price_ladder_type: BetfairPriceLadderType
    price: Decimal
    provider_increment_valid: bool = field(default=True, init=False)
    market_metadata_authority_proven: bool = field(default=False, init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _exact_enum(
            self.market_betting_type,
            BetfairMarketBettingType,
            "market_betting_type",
        )
        _exact_enum(
            self.price_ladder_type,
            BetfairPriceLadderType,
            "price_ladder_type",
        )
        exact_price = _price(self.price)

        if self.market_betting_type is not BetfairMarketBettingType.ODDS:
            raise BetfairPriceDomainError(
                "standard LIMIT price check supports ODDS market semantics only"
            )

        _validate_price_increment(
            price=exact_price,
            price_ladder_type=self.price_ladder_type,
        )


def validate_betfair_standard_limit_price(
    *,
    market_betting_type: BetfairMarketBettingType,
    price_ladder_type: BetfairPriceLadderType,
    price: Decimal,
) -> BetfairStandardLimitPriceCheck:
    """Validate one ordinary Betfair LIMIT price against supplied ladder type."""

    return BetfairStandardLimitPriceCheck(
        market_betting_type=market_betting_type,
        price_ladder_type=price_ladder_type,
        price=price,
    )
