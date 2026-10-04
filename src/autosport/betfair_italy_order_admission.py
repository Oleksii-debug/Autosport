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
from decimal import Decimal
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
            "ruleset_satisfied_unbound",
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

    def __post_init__(
        self,
        _state_type=ItalianLimitAdmissionState,
        _decimal_type=Decimal,
        _error_type=ItalianOrderAdmissionError,
    ) -> None:
        if type(self.state) is not _state_type:
            raise _error_type(
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
            type(value) is not _decimal_type
            or not value.is_finite()
            or value <= 0
            for value in self.preselected_returns_eur
        ):
            raise _error_type(
                "preselected_returns_eur must contain positive finite Decimals"
            )
        if (
            self.state is _state_type.RULESET_SATISFIED_UNBOUND
            and self.reason_codes
        ):
            raise _error_type(
                "RULESET_SATISFIED_UNBOUND cannot carry rejection reasons"
            )
        if (
            self.state is _state_type.REJECTED
            and not self.reason_codes
        ):
            raise _error_type(
                "REJECTED result requires a reason"
            )

    @property
    def ruleset_satisfied_unbound(
        self,
        _state_type=ItalianLimitAdmissionState,
    ) -> bool:
        return self.state is _state_type.RULESET_SATISFIED_UNBOUND

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


def _positive_decimal(value: object, field: str) -> Decimal:
    decimal_type = Decimal
    if (
        type(value) is not decimal_type
        or not value.is_finite()
        or value <= 0
    ):
        raise ItalianOrderAdmissionError(
            f"{field} must be an exact positive finite Decimal"
        )
    parts = value.as_tuple()
    if (
        len(parts.digits) > _MAX_DECIMAL_DIGITS
        or abs(parts.exponent) > _MAX_ABS_EXPONENT
    ):
        raise ItalianOrderAdmissionError(
            f"{field} exceeds bounded Decimal shape"
        )
    return value


def _build_italian_limit_evaluator():
    """Capture the diagnostic rule authority graph once at module load."""

    instruction_type = ItalianLimitInstruction
    result_type = ItalianLimitBatchAdmission
    state_type = ItalianLimitAdmissionState
    error_type = ItalianOrderAdmissionError
    decimal_type = Decimal
    fraction_type = Fraction
    max_decimal_digits = _MAX_DECIMAL_DIGITS
    max_abs_exponent = _MAX_ABS_EXPONENT

    selection_id_slot = instruction_type.__dict__["selection_id"]
    side_slot = instruction_type.__dict__["side"]
    size_slot = instruction_type.__dict__["size"]
    price_slot = instruction_type.__dict__["price"]
    target_slot = instruction_type.__dict__["bet_target_type"]

    one = decimal_type("1")
    back_min = decimal_type("2.00")
    back_increment = decimal_type("0.50")
    lay_min = decimal_type("0.50")
    max_return = decimal_type("10000.00")

    def validate_decimal(value: object, field: str) -> Decimal:
        if (
            type(value) is not decimal_type
            or not value.is_finite()
            or value <= 0
        ):
            raise error_type(
                f"{field} must be an exact positive finite Decimal"
            )
        parts = value.as_tuple()
        if (
            len(parts.digits) > max_decimal_digits
            or abs(parts.exponent) > max_abs_exponent
        ):
            raise error_type(f"{field} exceeds bounded Decimal shape")
        return value

    def snapshot_instruction(
        instruction: ItalianLimitInstruction,
    ) -> tuple[int, str, Decimal, Decimal, str | None]:
        """Read one exact DTO once, then validate the immutable local snapshot."""

        owner = type(instruction)
        selection_id = selection_id_slot.__get__(instruction, owner)
        side = side_slot.__get__(instruction, owner)
        size = size_slot.__get__(instruction, owner)
        price = price_slot.__get__(instruction, owner)
        target = target_slot.__get__(instruction, owner)

        if type(selection_id) is not int or selection_id <= 0:
            raise error_type("selection_id must be a positive integer")
        if type(side) is not str or side not in {"BACK", "LAY"}:
            raise error_type("side must be exactly BACK or LAY")
        size = validate_decimal(size, "size")
        price = validate_decimal(price, "price")
        if price <= one:
            raise error_type("price must be greater than 1")
        if target is not None and (
            type(target) is not str
            or target not in {"PAYOUT", "BACKERS_PROFIT"}
        ):
            raise error_type(
                "bet_target_type must be PAYOUT, BACKERS_PROFIT, or None"
            )
        return selection_id, side, size, price, target

    def is_multiple(value: Decimal, increment: Decimal) -> bool:
        value_fraction = fraction_type(value)
        increment_fraction = fraction_type(increment)
        quotient = value_fraction / increment_fraction
        return quotient.denominator == 1

    def exact_multiply(left: Decimal, right: Decimal) -> Decimal:
        """Multiply Decimal tuples exactly, independent of ambient context."""

        left_parts = left.as_tuple()
        right_parts = right.as_tuple()

        left_coefficient = 0
        for digit in left_parts.digits:
            left_coefficient = (left_coefficient * 10) + digit
        right_coefficient = 0
        for digit in right_parts.digits:
            right_coefficient = (right_coefficient * 10) + digit

        product = left_coefficient * right_coefficient
        product_digits = tuple(int(character) for character in str(product))
        exponent = left_parts.exponent + right_parts.exponent
        result = decimal_type((0, product_digits, exponent))
        if not result.is_finite() or result <= 0:
            raise error_type(
                "preselected return is not a positive finite Decimal"
            )
        return result

    def evaluate_italian_limit_batch(
        instructions: tuple[ItalianLimitInstruction, ...],
    ) -> ItalianLimitBatchAdmission:
        """Evaluate only the represented Betfair Italy standard-LIMIT rule slice.

        A no-reason result is RULESET_SATISFIED_UNBOUND, not ADMISSIBLE.
        Positive provider admission requires separate product-owned current-rule,
        authenticated-jurisdiction, account-currency, market, risk/funds and
        other execution authorities.
        """

        if type(instructions) is not tuple:
            raise error_type("instructions must be an immutable exact tuple")
        if not instructions:
            return result_type(
                state_type.REJECTED,
                ("EMPTY_BATCH",),
                (),
            )

        # Bound work before inspecting caller-controlled members. The provider
        # request shape is already invalid above 50, so scanning arbitrary
        # over-limit payloads adds no diagnostic authority.
        if len(instructions) > 50:
            return result_type(
                state_type.REJECTED,
                ("TOO_MANY_INSTRUCTIONS",),
                (),
            )

        if any(type(item) is not instruction_type for item in instructions):
            raise error_type(
                "instructions must contain exact ItalianLimitInstruction values"
            )

        snapshots = tuple(snapshot_instruction(item) for item in instructions)
        reasons: list[str] = []

        sides = {snapshot[1] for snapshot in snapshots}
        if len(sides) > 1:
            reasons.append("MIXED_BACK_LAY_BATCH")

        returns: list[Decimal] = []
        for index, snapshot in enumerate(snapshots):
            _selection_id, side, size, price, target = snapshot
            prefix = f"I{index}:"
            if target is not None:
                # .it does not support Betfair target sizing. Do not reinterpret
                # target-mode numeric fields as standard backer's-stake economics.
                reasons.append(prefix + "TARGET_MODE_UNAVAILABLE_IT")
                continue

            if side == "BACK":
                if size < back_min:
                    reasons.append(prefix + "BACK_STAKE_BELOW_EUR_2")
                if not is_multiple(size, back_increment):
                    reasons.append(
                        prefix + "BACK_STAKE_NOT_EUR_0_50_INCREMENT"
                    )
            else:
                if size < lay_min:
                    reasons.append(
                        prefix + "LAY_BACKER_STAKE_BELOW_EUR_0_50"
                    )

            # Represented rule arithmetic only: for standard LIMIT both BACK and
            # LAY returned amount at submitted price is backer's stake * price.
            preselected_return = exact_multiply(size, price)
            returns.append(preselected_return)
            if preselected_return > max_return:
                reasons.append(
                    prefix + "PRESELECTED_RETURN_EXCEEDS_EUR_10000"
                )

        state = (
            state_type.REJECTED
            if reasons
            else state_type.RULESET_SATISFIED_UNBOUND
        )
        return result_type(state, tuple(reasons), tuple(returns))

    return evaluate_italian_limit_batch


evaluate_italian_limit_batch = _build_italian_limit_evaluator()
del _build_italian_limit_evaluator
