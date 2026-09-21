"""Exact Betfair order capital-reserve arithmetic.

This module is a narrow provider-economic contract. It converts documented
Betfair order sizing semantics into capital that must be reserved before an
order could be considered for execution.

Unquantized ratios are retained as exact rational values. Decimal is used only
for caller inputs and the final currency-quantized reserve, so repeating target
ratios cannot silently under-reserve because of the active Decimal context.

It deliberately owns no admission limits, odds ladder, provider transport,
execution acknowledgement, reconciliation, retry, jurisdiction policy, or
real-money authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction


class BetfairOrderLiabilityError(ValueError):
    """The requested sizing semantics are unsupported or not exact."""


class BetfairOrderSide(str, Enum):
    BACK = "BACK"
    LAY = "LAY"


class BetfairOrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET_ON_CLOSE = "MARKET_ON_CLOSE"
    LIMIT_ON_CLOSE = "LIMIT_ON_CLOSE"


class BetfairBetTargetType(str, Enum):
    PAYOUT = "PAYOUT"
    BACKERS_PROFIT = "BACKERS_PROFIT"


class BetfairMarketBettingType(str, Enum):
    ODDS = "ODDS"
    LINE = "LINE"
    RANGE = "RANGE"
    ASIAN_HANDICAP_DOUBLE_LINE = "ASIAN_HANDICAP_DOUBLE_LINE"
    ASIAN_HANDICAP_SINGLE_LINE = "ASIAN_HANDICAP_SINGLE_LINE"
    FIXED_ODDS = "FIXED_ODDS"


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise BetfairOrderLiabilityError(f"{name} must be Decimal")
    if not value.is_finite() or value <= 0:
        raise BetfairOrderLiabilityError(f"{name} must be finite and > 0")
    return value


def _enum(value: object, enum_type: type[Enum], name: str) -> Enum:
    if type(value) is not enum_type:
        raise BetfairOrderLiabilityError(
            f"{name} must be {enum_type.__name__}"
        )
    return value


def _fraction(value: Decimal) -> Fraction:
    return Fraction(value)


def _fraction_to_decimal_exact(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise BetfairOrderLiabilityError(
            "exact Decimal conversion requires a finite decimal fraction"
        )
    scale = max(twos, fives)
    scaled = (
        value.numerator
        * (2 ** (scale - twos))
        * (5 ** (scale - fives))
    )
    sign = int(scaled < 0)
    digits = tuple(int(ch) for ch in str(abs(scaled))) or (0,)
    return Decimal((sign, digits, -scale))


def _round_fraction_up(value: Fraction, quantum: Decimal) -> Decimal:
    quantum_fraction = _fraction(quantum)
    numerator = value.numerator * quantum_fraction.denominator
    denominator = value.denominator * quantum_fraction.numerator
    units = (numerator + denominator - 1) // denominator
    rounded_fraction = units * quantum_fraction
    rounded = _fraction_to_decimal_exact(rounded_fraction)
    if Fraction(rounded) < value:
        raise BetfairOrderLiabilityError(
            "currency reserve rounding lost exact rational coverage"
        )
    return rounded


@dataclass(frozen=True, slots=True)
class _ReserveCalculation:
    reserve: Decimal
    raw_reserve: Fraction
    backer_stake_equivalent: Fraction | None


def _derive_components(
    *,
    side: BetfairOrderSide,
    market_betting_type: BetfairMarketBettingType,
    order_type: BetfairOrderType,
    price: Decimal | None,
    size: Decimal | None,
    liability: Decimal | None,
    target_type: BetfairBetTargetType | None,
    target_size: Decimal | None,
    currency_quantum: Decimal | None,
    each_way: bool,
) -> _ReserveCalculation:
    _enum(side, BetfairOrderSide, "side")
    _enum(
        market_betting_type,
        BetfairMarketBettingType,
        "market_betting_type",
    )
    if market_betting_type is not BetfairMarketBettingType.ODDS:
        raise BetfairOrderLiabilityError(
            "only ODDS market betting semantics are supported"
        )
    _enum(order_type, BetfairOrderType, "order_type")
    if type(each_way) is not bool:
        raise BetfairOrderLiabilityError("each_way must be bool")

    if order_type in (
        BetfairOrderType.MARKET_ON_CLOSE,
        BetfairOrderType.LIMIT_ON_CLOSE,
    ):
        if (
            size is not None
            or target_type is not None
            or target_size is not None
            or currency_quantum is not None
            or each_way
        ):
            raise BetfairOrderLiabilityError(
                "BSP close orders require explicit liability only"
            )
        amount = _decimal(liability, "liability")
        if order_type is BetfairOrderType.LIMIT_ON_CLOSE:
            limit_price = _decimal(price, "price")
            if limit_price <= Decimal("1"):
                raise BetfairOrderLiabilityError("price must be > 1")
        elif price is not None:
            raise BetfairOrderLiabilityError(
                "MARKET_ON_CLOSE has no preselected price"
            )
        raw = _fraction(amount)
        return _ReserveCalculation(
            reserve=amount,
            raw_reserve=raw,
            backer_stake_equivalent=(
                raw if side is BetfairOrderSide.BACK else None
            ),
        )

    if liability is not None:
        raise BetfairOrderLiabilityError(
            "standard LIMIT uses size or bet target, not liability"
        )

    limit_price = _decimal(price, "price")
    if limit_price <= Decimal("1"):
        raise BetfairOrderLiabilityError("price must be > 1")
    price_fraction = _fraction(limit_price)

    if target_type is None and target_size is None:
        if currency_quantum is not None:
            raise BetfairOrderLiabilityError(
                "standard LIMIT must not provide currency_quantum"
            )
        backer_stake_decimal = _decimal(size, "size")
        backer_stake = _fraction(backer_stake_decimal)
        if each_way:
            if side is not BetfairOrderSide.BACK:
                raise BetfairOrderLiabilityError(
                    "LAY EACH_WAY liability is intentionally unsupported"
                )
            raw_reserve = backer_stake * 2
        elif side is BetfairOrderSide.BACK:
            raw_reserve = backer_stake
        else:
            raw_reserve = backer_stake * (price_fraction - 1)
        reserve = _fraction_to_decimal_exact(raw_reserve)
        return _ReserveCalculation(
            reserve=reserve,
            raw_reserve=raw_reserve,
            backer_stake_equivalent=backer_stake,
        )

    if size is not None:
        raise BetfairOrderLiabilityError(
            "target LIMIT must not also provide standard size"
        )
    if each_way:
        raise BetfairOrderLiabilityError(
            "target-mode EACH_WAY liability is intentionally unsupported"
        )
    _enum(target_type, BetfairBetTargetType, "target_type")
    target = _decimal(target_size, "target_size")
    quantum = _decimal(currency_quantum, "currency_quantum")
    target_fraction = _fraction(target)

    if target_type is BetfairBetTargetType.PAYOUT:
        backer_stake = target_fraction / price_fraction
    else:
        backer_stake = target_fraction / (price_fraction - 1)

    if side is BetfairOrderSide.BACK:
        raw_reserve = backer_stake
    else:
        raw_reserve = backer_stake * (price_fraction - 1)

    reserve = _round_fraction_up(raw_reserve, quantum)
    return _ReserveCalculation(
        reserve=reserve,
        raw_reserve=raw_reserve,
        backer_stake_equivalent=backer_stake,
    )


@dataclass(frozen=True, slots=True)
class BetfairOrderReserve:
    """Self-validating exact provider-economic reserve calculation."""

    side: BetfairOrderSide
    market_betting_type: BetfairMarketBettingType
    order_type: BetfairOrderType
    price: Decimal | None
    size: Decimal | None
    liability: Decimal | None
    target_type: BetfairBetTargetType | None
    target_size: Decimal | None
    currency_quantum: Decimal | None
    each_way: bool
    reserve: Decimal
    raw_reserve: Fraction
    backer_stake_equivalent: Fraction | None
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if self.execution_authority is not False:
            raise BetfairOrderLiabilityError(
                "order reserve never grants execution authority"
            )
        expected = _derive_components(
            side=self.side,
            market_betting_type=self.market_betting_type,
            order_type=self.order_type,
            price=self.price,
            size=self.size,
            liability=self.liability,
            target_type=self.target_type,
            target_size=self.target_size,
            currency_quantum=self.currency_quantum,
            each_way=self.each_way,
        )
        if type(self.reserve) is not Decimal or self.reserve != expected.reserve:
            raise BetfairOrderLiabilityError(
                "reserve does not match exact derivation inputs"
            )
        if (
            type(self.raw_reserve) is not Fraction
            or self.raw_reserve != expected.raw_reserve
        ):
            raise BetfairOrderLiabilityError(
                "raw_reserve does not match exact derivation inputs"
            )
        if self.backer_stake_equivalent is None:
            backer_stake_matches = expected.backer_stake_equivalent is None
        else:
            backer_stake_matches = (
                type(self.backer_stake_equivalent) is Fraction
                and self.backer_stake_equivalent
                == expected.backer_stake_equivalent
            )
        if not backer_stake_matches:
            raise BetfairOrderLiabilityError(
                "backer_stake_equivalent does not match exact derivation inputs"
            )


def derive_betfair_order_reserve(
    *,
    side: BetfairOrderSide,
    market_betting_type: BetfairMarketBettingType,
    order_type: BetfairOrderType,
    price: Decimal | None = None,
    size: Decimal | None = None,
    liability: Decimal | None = None,
    target_type: BetfairBetTargetType | None = None,
    target_size: Decimal | None = None,
    currency_quantum: Decimal | None = None,
    each_way: bool = False,
) -> BetfairOrderReserve:
    """Derive capital reserve for one documented Betfair sizing mode.

    Supported provider semantics:
    - standard LIMIT: API 'size' is backer's stake;
    - target LIMIT: PAYOUT / BACKERS_PROFIT is converted at the submitted
      limit price using exact rational arithmetic, then conservatively rounded
      upward to the caller-supplied currency quantum;
    - MARKET_ON_CLOSE / LIMIT_ON_CLOSE: API 'liability' is already the
      amount reserved (BACK stake or LAY max loss);
    - EACH_WAY: only standard-size BACK LIMIT is admitted here; documented
      potential liability is 'size * 2'.

    Unsupported combinations fail closed rather than guessing.
    """
    calculation = _derive_components(
        side=side,
        market_betting_type=market_betting_type,
        order_type=order_type,
        price=price,
        size=size,
        liability=liability,
        target_type=target_type,
        target_size=target_size,
        currency_quantum=currency_quantum,
        each_way=each_way,
    )
    return BetfairOrderReserve(
        side=side,
        market_betting_type=market_betting_type,
        order_type=order_type,
        price=price,
        size=size,
        liability=liability,
        target_type=target_type,
        target_size=target_size,
        currency_quantum=currency_quantum,
        each_way=each_way,
        reserve=calculation.reserve,
        raw_reserve=calculation.raw_reserve,
        backer_stake_equivalent=calculation.backer_stake_equivalent,
    )
