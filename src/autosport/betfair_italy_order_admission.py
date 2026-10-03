"""Truth-bounded request-level rule projection for Betfair Italy LIMIT orders.

This module evaluates the represented .it Exchange rules against an immutable
standard-size LIMIT batch. It deliberately does not prove that the caller is an
Italy-authenticated session, that EUR is the current authenticated account
currency, or that the represented provider rules are current at decision time.

A rule-set match is therefore diagnostic/preflight structure only. It is never
market admissibility, provider-write permission, or real-money authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from fractions import Fraction
from operator import attrgetter

MAX_PLACE_INSTRUCTIONS = 50
BACK_MIN_STAKE_EUR = Decimal("2.00")
BACK_STAKE_INCREMENT_EUR = Decimal("0.50")
LAY_MIN_BACKER_STAKE_EUR = Decimal("0.50")
MAX_PRESELECTED_RETURN_EUR = Decimal("10000.00")

# Bound all caller-controlled Decimal shapes before Fraction construction or
# arithmetic. These limits are far beyond legitimate Exchange order values and
# prevent pathological exponents/coefficients from becoming a resource sink.
_MAX_DECIMAL_DIGITS = 64
_MAX_ABS_EXPONENT = 18


class ItalianOrderAdmissionError(ValueError):
    """Raised when an order projection is malformed or outside this contract."""


class ItalianLimitAdmissionState(str, Enum):
    RULESET_SATISFIED_UNBOUND = "RULESET_SATISFIED_UNBOUND"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ItalianLimitInstruction:
    """Minimal standard-size LIMIT projection needed by represented .it rules."""

    selection_id: int
    side: str
    size: Decimal
    price: Decimal
    bet_target_type: str | None = None

    def __post_init__(self) -> None:
        if type(self.selection_id) is not int or self.selection_id <= 0:
            raise ItalianOrderAdmissionError(
                "selection_id must be a positive integer"
            )
        if type(self.side) is not str or self.side not in {"BACK", "LAY"}:
            raise ItalianOrderAdmissionError("side must be exactly BACK or LAY")
        _positive_decimal(self.size, "size")
        _positive_decimal(self.price, "price")
        if self.price <= Decimal("1"):
            raise ItalianOrderAdmissionError("price must be greater than 1")
        if self.bet_target_type is not None and (
            type(self.bet_target_type) is not str
            or self.bet_target_type not in {"PAYOUT", "BACKERS_PROFIT"}
        ):
            raise ItalianOrderAdmissionError(
                "bet_target_type must be PAYOUT, BACKERS_PROFIT, or None"
            )


def _build_italian_limit_batch_admission_meta():
    """Seal hard-false provider/execution claims on diagnostic results."""

    sealed_classes: set[type] = set()
    protected_names = frozenset(
        {
            "__dataclass_fields__",
            "__init__",
            "__post_init__",
            "admissible",
            "jurisdiction_bound",
            "account_currency_bound",
            "current_provider_rules_proven",
            "execution_authorized",
            "real_money_execution",
            "_admissible_constant",
            "_jurisdiction_bound_constant",
            "_account_currency_bound_constant",
            "_current_provider_rules_proven_constant",
            "_execution_authorized_constant",
            "_real_money_execution_constant",
        }
    )

    class _ItalianLimitBatchAdmissionMeta(type):
        def __setattr__(cls, name: str, value: object) -> None:
            if cls in sealed_classes and name in protected_names:
                raise TypeError(
                    "Italian LIMIT diagnostic authority surface is sealed: " + name
                )
            super().__setattr__(name, value)

        def __delattr__(cls, name: str) -> None:
            if cls in sealed_classes and name in protected_names:
                raise TypeError(
                    "Italian LIMIT diagnostic authority surface is sealed: " + name
                )
            super().__delattr__(name)

        @classmethod
        def seal(mcls, cls: type) -> None:
            sealed_classes.add(cls)

    return _ItalianLimitBatchAdmissionMeta


_ItalianLimitBatchAdmissionMeta = _build_italian_limit_batch_admission_meta()
del _build_italian_limit_batch_admission_meta


@dataclass(frozen=True, slots=True)
class ItalianLimitBatchAdmission(metaclass=_ItalianLimitBatchAdmissionMeta):
    """Result for represented rule checks, never a provider admission token."""

    state: ItalianLimitAdmissionState
    reason_codes: tuple[str, ...]
    preselected_returns_eur: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if type(self.state) is not ItalianLimitAdmissionState:
            raise ItalianOrderAdmissionError(
                "state must be exact ItalianLimitAdmissionState"
            )
        if type(self.reason_codes) is not tuple or any(
            type(reason) is not str or not reason
            for reason in self.reason_codes
        ):
            raise ItalianOrderAdmissionError(
                "reason_codes must be an exact tuple of non-empty strings"
            )
        if type(self.preselected_returns_eur) is not tuple or any(
            type(value) is not Decimal
            or not value.is_finite()
            or value <= 0
            for value in self.preselected_returns_eur
        ):
            raise ItalianOrderAdmissionError(
                "preselected_returns_eur must contain positive finite Decimals"
            )
        if (
            self.state is ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND
            and self.reason_codes
        ):
            raise ItalianOrderAdmissionError(
                "RULESET_SATISFIED_UNBOUND cannot carry rejection reasons"
            )
        if (
            self.state is ItalianLimitAdmissionState.REJECTED
            and not self.reason_codes
        ):
            raise ItalianOrderAdmissionError(
                "REJECTED result requires a reason"
            )

    @property
    def ruleset_satisfied_unbound(self) -> bool:
        return (
            self.state
            is ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND
        )

    # These claims are intentionally hard-false. Keep their getters out of
    # mutable Python bytecode and their backing values off instance slots.
    _admissible_constant = False
    _jurisdiction_bound_constant = False
    _account_currency_bound_constant = False
    _current_provider_rules_proven_constant = False
    _execution_authorized_constant = False
    _real_money_execution_constant = False

    admissible = property(attrgetter("_admissible_constant"))
    jurisdiction_bound = property(attrgetter("_jurisdiction_bound_constant"))
    account_currency_bound = property(
        attrgetter("_account_currency_bound_constant")
    )
    current_provider_rules_proven = property(
        attrgetter("_current_provider_rules_proven_constant")
    )
    execution_authorized = property(attrgetter("_execution_authorized_constant"))
    real_money_execution = property(attrgetter("_real_money_execution_constant"))


_ItalianLimitBatchAdmissionMeta.seal(ItalianLimitBatchAdmission)


def evaluate_italian_limit_batch(
    instructions: tuple[ItalianLimitInstruction, ...],
) -> ItalianLimitBatchAdmission:
    """Evaluate only the represented Betfair Italy standard-LIMIT rule slice.

    A no-reason result is RULESET_SATISFIED_UNBOUND, not ADMISSIBLE. Positive
    provider admission requires separate product-owned current-rule,
    authenticated-jurisdiction, account-currency, market, risk/funds and other
    execution authorities.
    """

    if type(instructions) is not tuple:
        raise ItalianOrderAdmissionError(
            "instructions must be an immutable exact tuple"
        )
    if any(type(item) is not ItalianLimitInstruction for item in instructions):
        raise ItalianOrderAdmissionError(
            "instructions must contain exact ItalianLimitInstruction values"
        )

    reasons: list[str] = []
    if not instructions:
        return ItalianLimitBatchAdmission(
            ItalianLimitAdmissionState.REJECTED,
            ("EMPTY_BATCH",),
            (),
        )
    if len(instructions) > 50:
        reasons.append("TOO_MANY_INSTRUCTIONS")

    sides = {instruction.side for instruction in instructions}
    if len(sides) > 1:
        reasons.append("MIXED_BACK_LAY_BATCH")

    returns: list[Decimal] = []
    for index, instruction in enumerate(instructions):
        prefix = f"I{index}:"
        if instruction.bet_target_type is not None:
            # .it does not support Betfair target sizing. Do not reinterpret
            # target-mode numeric fields as standard backer's-stake economics.
            reasons.append(prefix + "TARGET_MODE_UNAVAILABLE_IT")
            continue

        if instruction.side == "BACK":
            if instruction.size < Decimal("2.00"):
                reasons.append(prefix + "BACK_STAKE_BELOW_EUR_2")
            if not _is_multiple(
                instruction.size, Decimal("0.50")
            ):
                reasons.append(
                    prefix + "BACK_STAKE_NOT_EUR_0_50_INCREMENT"
                )
        else:
            if instruction.size < Decimal("0.50"):
                reasons.append(
                    prefix + "LAY_BACKER_STAKE_BELOW_EUR_0_50"
                )

        # Represented rule arithmetic only: for standard LIMIT both BACK and
        # LAY returned amount at submitted price is backer's stake * price.
        preselected_return = _exact_multiply(
            instruction.size, instruction.price
        )
        returns.append(preselected_return)
        if preselected_return > Decimal("10000.00"):
            reasons.append(
                prefix + "PRESELECTED_RETURN_EXCEEDS_EUR_10000"
            )

    state = (
        ItalianLimitAdmissionState.REJECTED
        if reasons
        else ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND
    )
    return ItalianLimitBatchAdmission(
        state, tuple(reasons), tuple(returns)
    )


def _positive_decimal(value: object, field: str) -> Decimal:
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value <= 0
    ):
        raise ItalianOrderAdmissionError(
            f"{field} must be an exact positive finite Decimal"
        )
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    if (
        len(digits) > _MAX_DECIMAL_DIGITS
        or abs(exponent) > _MAX_ABS_EXPONENT
    ):
        raise ItalianOrderAdmissionError(
            f"{field} exceeds bounded Decimal shape"
        )
    return value


def _is_multiple(value: Decimal, increment: Decimal) -> bool:
    # Inputs are shape-bounded before this exact-rational conversion.
    value_fraction = Fraction(value)
    increment_fraction = Fraction(increment)
    quotient = value_fraction / increment_fraction
    return quotient.denominator == 1


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    """Multiply bounded finite Decimals independently of ambient precision."""

    left_digits = max(1, len(left.as_tuple().digits))
    right_digits = max(1, len(right.as_tuple().digits))
    with localcontext() as context:
        context.prec = left_digits + right_digits + 2
        result = left * right
    if not result.is_finite() or result <= 0:
        raise ItalianOrderAdmissionError(
            "preselected return is not a positive finite Decimal"
        )
    return result
