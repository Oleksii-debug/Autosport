"""Exact Betfair order capital-reserve arithmetic.

This module is a narrow provider-economic contract.  It converts documented
Betfair order sizing semantics into exact Decimal capital that must be reserved
*before* an order could be considered for execution.

It deliberately owns no admission limits, odds ladder, provider transport,
execution acknowledgement, reconciliation, retry, jurisdiction policy, or
real-money authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


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


def _round_up(value: Decimal, quantum: Decimal) -> Decimal:
    """Round to a quantum without letting Decimal context under-reserve."""

    value_num, value_den = value.as_integer_ratio()
    quantum_num, quantum_den = quantum.as_integer_ratio()
    numerator = value_num * quantum_den
    denominator = value_den * quantum_num
    units = (numerator + denominator - 1) // denominator
    return Decimal(units) * quantum


@dataclass(frozen=True, slots=True)
class BetfairOrderReserve:
    """Exact reserve derived from provider sizing semantics."""

    side: BetfairOrderSide
    order_type: BetfairOrderType
    reserve: Decimal
    unrounded_reserve: Decimal
    backer_stake_equivalent: Decimal | None
    target_type: BetfairBetTargetType | None
    target_size: Decimal | None
    each_way: bool
    execution_authority: bool = False

    def __post_init__(self) -> None:
        _enum(self.side, BetfairOrderSide, "side")
        _enum(self.order_type, BetfairOrderType, "order_type")
        _decimal(self.reserve, "reserve")
        _decimal(self.unrounded_reserve, "unrounded_reserve")
        if self.reserve < self.unrounded_reserve:
            raise BetfairOrderLiabilityError(
                "reserve must not be below unrounded_reserve"
            )
        if self.backer_stake_equivalent is not None:
            _decimal(
                self.backer_stake_equivalent,
                "backer_stake_equivalent",
            )
        if self.target_type is not None:
            _enum(
                self.target_type,
                BetfairBetTargetType,
                "target_type",
            )
        if self.target_size is not None:
            _decimal(self.target_size, "target_size")
        if type(self.each_way) is not bool:
            raise BetfairOrderLiabilityError("each_way must be bool")
        if self.execution_authority is not False:
            raise BetfairOrderLiabilityError(
                "order reserve never grants execution authority"
            )


def derive_betfair_order_reserve(
    *,
    side: BetfairOrderSide,
    order_type: BetfairOrderType,
    price: Decimal | None = None,
    size: Decimal | None = None,
    liability: Decimal | None = None,
    target_type: BetfairBetTargetType | None = None,
    target_size: Decimal | None = None,
    currency_quantum: Decimal | None = None,
    each_way: bool = False,
) -> BetfairOrderReserve:
    """Derive exact capital reserve for one Betfair order.

    Supported provider semantics:
    - standard LIMIT: API ``size`` is backer's stake;
    - target LIMIT: PAYOUT / BACKERS_PROFIT is converted at the submitted
      limit price, then reserve is conservatively rounded upward to the
      caller-supplied currency quantum;
    - MARKET_ON_CLOSE / LIMIT_ON_CLOSE: API ``liability`` is already the
      amount reserved (BACK stake or LAY max loss);
    - EACH_WAY: only standard-size BACK LIMIT is admitted here; documented
      potential liability is ``size * 2``.

    Unsupported combinations fail closed rather than guessing.
    """

    _enum(side, BetfairOrderSide, "side")
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
        return BetfairOrderReserve(
            side=side,
            order_type=order_type,
            reserve=amount,
            unrounded_reserve=amount,
            backer_stake_equivalent=(
                amount if side is BetfairOrderSide.BACK else None
            ),
            target_type=None,
            target_size=None,
            each_way=False,
        )

    if liability is not None:
        raise BetfairOrderLiabilityError(
            "standard LIMIT uses size or bet target, not liability"
        )

    limit_price = _decimal(price, "price")
    if limit_price <= Decimal("1"):
        raise BetfairOrderLiabilityError("price must be > 1")

    if target_type is None and target_size is None:
        backer_stake = _decimal(size, "size")
        if each_way:
            if side is not BetfairOrderSide.BACK:
                raise BetfairOrderLiabilityError(
                    "LAY EACH_WAY liability is intentionally unsupported"
                )
            reserve = backer_stake * Decimal("2")
        elif side is BetfairOrderSide.BACK:
            reserve = backer_stake
        else:
            reserve = backer_stake * (limit_price - Decimal("1"))
        return BetfairOrderReserve(
            side=side,
            order_type=order_type,
            reserve=reserve,
            unrounded_reserve=reserve,
            backer_stake_equivalent=backer_stake,
            target_type=None,
            target_size=None,
            each_way=each_way,
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

    if target_type is BetfairBetTargetType.PAYOUT:
        backer_stake = target / limit_price
    else:
        backer_stake = target / (limit_price - Decimal("1"))

    if side is BetfairOrderSide.BACK:
        raw_reserve = backer_stake
    else:
        raw_reserve = backer_stake * (limit_price - Decimal("1"))

    reserve = _round_up(raw_reserve, quantum)
    return BetfairOrderReserve(
        side=side,
        order_type=order_type,
        reserve=reserve,
        unrounded_reserve=raw_reserve,
        backer_stake_equivalent=backer_stake,
        target_type=target_type,
        target_size=target,
        each_way=False,
    )
