"""Fail-closed Betfair Italy BSP/QI order-admission contract.

This module is deliberately pure and non-authorizing.  It models only the
provider constraints that can be decided before a Betfair Italy BSP order is
submitted.  It does not place orders, inspect balances, select odds, reconcile
fills, or grant execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


BACK_BSP_MINIMUM_EUR = Decimal("2")
LAY_BSP_MINIMUM_LIABILITY_EUR = Decimal("10")
MAX_PRESELECTED_RETURN_EUR = Decimal("10000")


class BetfairItalyBspAdmissionError(ValueError):
    """The caller supplied a malformed BSP admission request."""


class BetfairItalyBspOrderType(str, Enum):
    MARKET_ON_CLOSE = "MARKET_ON_CLOSE"
    LIMIT_ON_CLOSE = "LIMIT_ON_CLOSE"


class BetfairItalyBspSide(str, Enum):
    BACK = "BACK"
    LAY = "LAY"


class BetfairItalyBspAdmissionStatus(str, Enum):
    ADMISSIBLE = "ADMISSIBLE"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


def _positive_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise BetfairItalyBspAdmissionError(f"{name} must be exact Decimal")
    if not value.is_finite() or value <= 0:
        raise BetfairItalyBspAdmissionError(f"{name} must be finite and > 0")
    return value


@dataclass(frozen=True, slots=True)
class BetfairItalyBspInstruction:
    """One Italy BSP instruction expressed in provider API liability terms.

    For BACK, ``liability_eur`` is the stake.  For LAY, it is the maximum loss
    (the API BSP liability field).  LIMIT_ON_CLOSE requires a pre-selected
    ``price_limit``; MARKET_ON_CLOSE intentionally has no pre-selected price.
    """

    order_type: BetfairItalyBspOrderType
    side: BetfairItalyBspSide
    liability_eur: Decimal
    price_limit: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.order_type, BetfairItalyBspOrderType):
            raise BetfairItalyBspAdmissionError(
                "order_type must be BetfairItalyBspOrderType"
            )
        if not isinstance(self.side, BetfairItalyBspSide):
            raise BetfairItalyBspAdmissionError(
                "side must be BetfairItalyBspSide"
            )
        _positive_decimal(self.liability_eur, "liability_eur")

        if self.order_type is BetfairItalyBspOrderType.MARKET_ON_CLOSE:
            if self.price_limit is not None:
                raise BetfairItalyBspAdmissionError(
                    "MARKET_ON_CLOSE must not carry price_limit"
                )
            return

        if self.price_limit is None:
            raise BetfairItalyBspAdmissionError(
                "LIMIT_ON_CLOSE requires price_limit"
            )
        price = _positive_decimal(self.price_limit, "price_limit")
        if price <= Decimal("1"):
            raise BetfairItalyBspAdmissionError(
                "price_limit must be > 1 for BSP return arithmetic"
            )


@dataclass(frozen=True, slots=True)
class BetfairItalyBspAdmission:
    status: BetfairItalyBspAdmissionStatus
    reason: str
    minimum_liability_eur: Decimal
    preselected_return_eur: Decimal | None
    execution_authority: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, BetfairItalyBspAdmissionStatus):
            raise BetfairItalyBspAdmissionError(
                "status must be BetfairItalyBspAdmissionStatus"
            )
        if type(self.reason) is not str or not self.reason:
            raise BetfairItalyBspAdmissionError("reason must be non-empty text")
        _positive_decimal(self.minimum_liability_eur, "minimum_liability_eur")
        if self.preselected_return_eur is not None:
            _positive_decimal(
                self.preselected_return_eur,
                "preselected_return_eur",
            )


def _minimum_for(side: BetfairItalyBspSide) -> Decimal:
    if side is BetfairItalyBspSide.BACK:
        return BACK_BSP_MINIMUM_EUR
    return LAY_BSP_MINIMUM_LIABILITY_EUR


def _limit_on_close_return(instruction: BetfairItalyBspInstruction) -> Decimal:
    price = instruction.price_limit
    if price is None:
        raise BetfairItalyBspAdmissionError(
            "LIMIT_ON_CLOSE requires price_limit"
        )
    if instruction.side is BetfairItalyBspSide.BACK:
        return instruction.liability_eur * price

    return instruction.liability_eur * price / (price - Decimal("1"))


def assess_betfair_italy_bsp_admission(
    instruction: BetfairItalyBspInstruction,
) -> BetfairItalyBspAdmission:
    """Evaluate the currently knowable Italy BSP admission boundary.

    ``ADMISSIBLE`` means this narrow contract found no provider-rule violation;
    it never means an order is authorized for execution.  MARKET_ON_CLOSE stays
    ``UNKNOWN`` after minimum checks because the EUR 10,000 pre-selected-return
    ceiling cannot be computed without a pre-selected price.
    """

    if not isinstance(instruction, BetfairItalyBspInstruction):
        raise BetfairItalyBspAdmissionError(
            "instruction must be BetfairItalyBspInstruction"
        )

    minimum = _minimum_for(instruction.side)
    if instruction.liability_eur < minimum:
        return BetfairItalyBspAdmission(
            status=BetfairItalyBspAdmissionStatus.REJECTED,
            reason="below_bsp_minimum",
            minimum_liability_eur=minimum,
            preselected_return_eur=None,
        )

    if instruction.order_type is BetfairItalyBspOrderType.MARKET_ON_CLOSE:
        return BetfairItalyBspAdmission(
            status=BetfairItalyBspAdmissionStatus.UNKNOWN,
            reason="moc_eur10000_cap_not_precomputable_without_preselected_odds",
            minimum_liability_eur=minimum,
            preselected_return_eur=None,
        )

    preselected_return = _limit_on_close_return(instruction)
    if preselected_return > MAX_PRESELECTED_RETURN_EUR:
        return BetfairItalyBspAdmission(
            status=BetfairItalyBspAdmissionStatus.REJECTED,
            reason="preselected_return_exceeds_eur10000",
            minimum_liability_eur=minimum,
            preselected_return_eur=preselected_return,
        )

    return BetfairItalyBspAdmission(
        status=BetfairItalyBspAdmissionStatus.ADMISSIBLE,
        reason="known_italy_bsp_constraints_satisfied",
        minimum_liability_eur=minimum,
        preselected_return_eur=preselected_return,
    )
