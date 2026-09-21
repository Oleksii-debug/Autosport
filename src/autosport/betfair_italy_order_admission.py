"""Fail-closed request-level admission for Betfair Italy LIMIT orders.

This module models only the Italian Exchange rules that can be decided from a
fully projected `placeOrders` LIMIT batch before provider I/O. It does not own
odds-ladder validity, account funds, BSP orders, provider writes,
acceptance/readback, settlement, or execution authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from fractions import Fraction

MAX_PLACE_INSTRUCTIONS = 50
BACK_MIN_STAKE_EUR = Decimal("2.00")
BACK_STAKE_INCREMENT_EUR = Decimal("0.50")
LAY_MIN_BACKER_STAKE_EUR = Decimal("0.50")
MAX_PRESELECTED_RETURN_EUR = Decimal("10000.00")


class ItalianOrderAdmissionError(ValueError):
    """Raised when an order projection is malformed or outside this contract."""


class ItalianLimitAdmissionState(str, Enum):
    ADMISSIBLE = "ADMISSIBLE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ItalianLimitInstruction:
    """Minimal provider-faithful LIMIT projection needed by the .it rules.

    For standard-size orders, ``size`` is Betfair's LIMIT ``size``: the
    backer's stake on both BACK and LAY instructions. ``bet_target_type`` is
    carried only so the .it guard can reject PAYOUT/BACKERS_PROFIT before
    applying standard-size stake/return semantics.
    """

    selection_id: int
    side: str
    size: Decimal
    price: Decimal
    bet_target_type: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.selection_id, int)
            or isinstance(self.selection_id, bool)
            or self.selection_id <= 0
        ):
            raise ItalianOrderAdmissionError("selection_id must be a positive integer")
        if self.side not in {"BACK", "LAY"}:
            raise ItalianOrderAdmissionError("side must be exactly BACK or LAY")
        _positive_decimal(self.size, "size")
        _positive_decimal(self.price, "price")
        if self.price <= 1:
            raise ItalianOrderAdmissionError("price must be greater than 1")
        if self.bet_target_type is not None:
            if self.bet_target_type not in {"PAYOUT", "BACKERS_PROFIT"}:
                raise ItalianOrderAdmissionError(
                    "bet_target_type must be PAYOUT, BACKERS_PROFIT, or None"
                )


@dataclass(frozen=True, slots=True)
class ItalianLimitBatchAdmission:
    state: ItalianLimitAdmissionState
    reason_codes: tuple[str, ...]
    preselected_returns_eur: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, ItalianLimitAdmissionState):
            raise ItalianOrderAdmissionError("state must be ItalianLimitAdmissionState")
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(reason, str) or not reason for reason in self.reason_codes
        ):
            raise ItalianOrderAdmissionError("reason_codes must be a tuple of strings")
        if not isinstance(self.preselected_returns_eur, tuple) or any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            for value in self.preselected_returns_eur
        ):
            raise ItalianOrderAdmissionError(
                "preselected_returns_eur must contain positive finite Decimals"
            )
        if self.state is ItalianLimitAdmissionState.ADMISSIBLE and self.reason_codes:
            raise ItalianOrderAdmissionError(
                "ADMISSIBLE result cannot carry rejection reasons"
            )
        if (
            self.state is ItalianLimitAdmissionState.REJECTED
            and not self.reason_codes
        ):
            raise ItalianOrderAdmissionError("REJECTED result requires a reason")

    @property
    def admissible(self) -> bool:
        return self.state is ItalianLimitAdmissionState.ADMISSIBLE

    @property
    def execution_authorized(self) -> bool:
        """This deterministic preflight never grants provider-write authority."""
        return False


def evaluate_italian_limit_batch(
    instructions: tuple[ItalianLimitInstruction, ...],
) -> ItalianLimitBatchAdmission:
    """Evaluate the documented Betfair Italy LIMIT request-level rules.

    The result is a deterministic preflight only. A positive result means the
    projected LIMIT batch satisfies the rules represented here; it does not
    prove account funds, market validity, provider acceptance, or permission to
    perform a real-money write.
    """

    if not isinstance(instructions, tuple):
        raise ItalianOrderAdmissionError("instructions must be an immutable tuple")
    if any(not isinstance(item, ItalianLimitInstruction) for item in instructions):
        raise ItalianOrderAdmissionError(
            "instructions must contain only ItalianLimitInstruction values"
        )

    reasons: list[str] = []
    if not instructions:
        return ItalianLimitBatchAdmission(
            ItalianLimitAdmissionState.REJECTED,
            ("EMPTY_BATCH",),
            (),
        )
    if len(instructions) > MAX_PLACE_INSTRUCTIONS:
        reasons.append("TOO_MANY_INSTRUCTIONS")

    sides = {instruction.side for instruction in instructions}
    if len(sides) > 1:
        reasons.append("MIXED_BACK_LAY_BATCH")

    returns: list[Decimal] = []
    for index, instruction in enumerate(instructions):
        prefix = f"I{index}:"
        if instruction.bet_target_type is not None:
            # .it does not support Betfair target sizing. Do not reinterpret
            # the target-mode numeric fields as standard backer's stake or
            # synthesize an EUR10k return from semantics that are unavailable.
            reasons.append(prefix + "TARGET_MODE_UNAVAILABLE_IT")
            continue

        if instruction.side == "BACK":
            if instruction.size < BACK_MIN_STAKE_EUR:
                reasons.append(prefix + "BACK_STAKE_BELOW_EUR_2")
            if not _is_multiple(instruction.size, BACK_STAKE_INCREMENT_EUR):
                reasons.append(prefix + "BACK_STAKE_NOT_EUR_0_50_INCREMENT")
        else:
            if instruction.size < LAY_MIN_BACKER_STAKE_EUR:
                reasons.append(prefix + "LAY_BACKER_STAKE_BELOW_EUR_0_50")

        # For BACK, total return including original stake is size * price.
        # For LAY, Betfair LIMIT size is the corresponding backer's stake /
        # layer's potential profit; liability is size * (price - 1), so
        # returned liability + profit is likewise size * price.
        preselected_return = _exact_multiply(instruction.size, instruction.price)
        returns.append(preselected_return)
        if preselected_return > MAX_PRESELECTED_RETURN_EUR:
            reasons.append(prefix + "PRESELECTED_RETURN_EXCEEDS_EUR_10000")

    state = (
        ItalianLimitAdmissionState.REJECTED
        if reasons
        else ItalianLimitAdmissionState.ADMISSIBLE
    )
    return ItalianLimitBatchAdmission(state, tuple(reasons), tuple(returns))


def _positive_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ItalianOrderAdmissionError(
            f"{field} must be a positive finite Decimal"
        )
    return value


def _is_multiple(value: Decimal, increment: Decimal) -> bool:
    value_fraction = Fraction(value)
    increment_fraction = Fraction(increment)
    quotient = value_fraction / increment_fraction
    return quotient.denominator == 1


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    """Multiply finite Decimals without depending on the ambient context."""

    left_digits = max(1, len(left.as_tuple().digits))
    right_digits = max(1, len(right.as_tuple().digits))
    with localcontext() as context:
        context.prec = left_digits + right_digits + 2
        return left * right
