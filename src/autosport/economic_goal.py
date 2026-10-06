"""Owner-bounded economic goal authority for Autosport.

This module defines a product-level goal/authority contract.  It deliberately does
not execute bets, mutate PaperBook state, or replace the executable risk policy.
Automatic actors may use :func:`validate_automatic_transition` only to prove that
one contract revision does not enlarge authority relative to the previous one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Final


class EconomicGoalContractError(ValueError):
    """Raised when an economic-goal contract or transition is non-canonical."""


class EconomicObjective(str, Enum):
    """Supported high-level economic objectives."""

    LONG_RUN_RISK_ADJUSTED_BANKROLL_GROWTH = (
        "long_run_risk_adjusted_bankroll_growth"
    )


class AutomationLevel(IntEnum):
    """Maximum execution autonomy permitted by the owner-level contract.

    Values intentionally match the canonical automation levels in Issue #353.
    The ordering is security-sensitive: a larger value represents strictly more
    money-moving authority. Merely selecting a level here does not create an
    execution path, and paper-mode automation is deliberately not encoded in this
    real-execution authority dimension.
    """

    ANALYSIS_ONLY = 0
    RECOMMENDATION = 1
    SUPERVISED_EXECUTION = 2
    BOUNDED_AUTONOMY = 3
    HIGHER_AUTONOMY = 4


_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_MAX_CANONICAL_TEXT_CHARS: Final = 512
_MAX_RESTRICTION_MEMBERS: Final = 1024


class _EconomicGoalContractMeta(type):
    """Seal the public automatic-successor method after canonical binding."""

    _AUTHORITY_NAMES: Final = frozenset(
        {
            "validate_automatic_successor",
            "_authority_operations_sealed",
        }
    )

    def __setattr__(
        cls,
        name: str,
        value: object,
        _authority_names=_AUTHORITY_NAMES,
    ) -> None:
        if (
            cls.__dict__.get("_authority_operations_sealed", False)
            and name in _authority_names
        ):
            raise TypeError(
                "economic-goal public authority operation binding is immutable"
            )
        super().__setattr__(name, value)

    def __delattr__(
        cls,
        name: str,
        _authority_names=_AUTHORITY_NAMES,
    ) -> None:
        if (
            cls.__dict__.get("_authority_operations_sealed", False)
            and name in _authority_names
        ):
            raise TypeError(
                "economic-goal public authority operation binding is immutable"
            )
        super().__delattr__(name)



def _build_contract_class_guard(name: str):
    """Block direct base-metaclass mutation of sealed contract authority names."""

    class _ContractClassGuard:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            for ancestor in instance.__mro__:
                if name in ancestor.__dict__:
                    binding = ancestor.__dict__[name]
                    break
            else:
                raise AttributeError(name)
            descriptor_get = getattr(binding, "__get__", None)
            if descriptor_get is None:
                return binding
            return descriptor_get(None, instance)

        def __set__(self, _instance, _value) -> None:
            raise TypeError(
                "economic-goal public authority operation binding is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise TypeError(
                "economic-goal public authority operation binding is immutable"
            )

    return _ContractClassGuard()


def _canonical_text(
    name: str,
    value: object,
    _max_chars=_MAX_CANONICAL_TEXT_CHARS,
    _error_type=EconomicGoalContractError,
) -> str:
    if type(value) is not str:
        raise _error_type(f"{name} must be a string")
    if not value or value != value.strip():
        raise _error_type(
            f"{name} must be a non-empty canonical string"
        )
    if len(value) > _max_chars:
        raise _error_type(
            f"{name} exceeds the canonical text size limit"
        )
    if "\x00" in value:
        raise _error_type(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _error_type(f"{name} must be valid UTF-8 text") from exc
    return value


def _decimal(
    name: str,
    value: object,
    _decimal_type=Decimal,
    _error_type=EconomicGoalContractError,
) -> Decimal:
    if type(value) is not _decimal_type:
        raise _error_type(f"{name} must be an exact Decimal")
    if not value.is_finite():
        raise _error_type(f"{name} must be finite")
    if value.is_zero() and value.is_signed():
        raise _error_type(f"{name} must not use signed zero")
    return value


def _fraction(
    name: str,
    value: object,
    _decimal_validator=_decimal,
    _zero=_ZERO,
    _one=_ONE,
    _error_type=EconomicGoalContractError,
) -> Decimal:
    result = _decimal_validator(name, value)
    if result < _zero or result > _one:
        raise _error_type(f"{name} must be between 0 and 1 inclusive")
    return result


def _nonnegative_decimal(
    name: str,
    value: object,
    _decimal_validator=_decimal,
    _zero=_ZERO,
    _error_type=EconomicGoalContractError,
) -> Decimal:
    result = _decimal_validator(name, value)
    if result < _zero:
        raise _error_type(f"{name} must be non-negative")
    return result


def _optional_nonnegative_decimal(
    name: str,
    value: object,
    _validator=_nonnegative_decimal,
) -> Decimal | None:
    if value is None:
        return None
    return _validator(name, value)


def _nonnegative_int(
    name: str,
    value: object,
    _error_type=EconomicGoalContractError,
) -> int:
    if type(value) is not int:
        raise _error_type(f"{name} must be a non-boolean integer")
    if value < 0:
        raise _error_type(f"{name} must be non-negative")
    return value


def _positive_int(
    name: str,
    value: object,
    _validator=_nonnegative_int,
    _error_type=EconomicGoalContractError,
) -> int:
    result = _validator(name, value)
    if result == 0:
        raise _error_type(f"{name} must be positive")
    return result


def _canonical_restrictions(
    name: str,
    value: object,
    _max_members=_MAX_RESTRICTION_MEMBERS,
    _text_validator=_canonical_text,
    _error_type=EconomicGoalContractError,
) -> frozenset[str]:
    if type(value) is not frozenset:
        raise _error_type(f"{name} must be a frozenset of strings")
    if len(value) > _max_members:
        raise _error_type(
            f"{name} exceeds the canonical restriction-count limit"
        )
    normalized: set[str] = set()
    for item in value:
        normalized.add(_text_validator(f"{name} member", item))
    if len(normalized) != len(value):
        # Defensive only; frozenset already removes exact duplicates.  Keep the
        # invariant explicit if its input contract ever changes.
        raise _error_type(f"{name} must contain unique members")
    return value


@dataclass(frozen=True, slots=True)
class EconomicGoalContract(metaclass=_EconomicGoalContractMeta):
    """Immutable owner-level economic objective and authority ceiling.

    Fractions are exact :class:`~decimal.Decimal` values in ``[0, 1]``.
    ``max_turnover_fraction`` may exceed 1 because turnover can legitimately be
    greater than bankroll over a bounded period.  ``max_stake_amount=None``
    means that this contract does not add an absolute-money cap; executable risk
    policy may still impose a tighter one.

    ``blocked_*`` sets are additive deny-lists.  They are intentionally separate
    from executable strategy/risk decisions so an agent cannot make a local
    opportunity look like authority to widen the owner's contract.
    """

    goal_id: str
    revision: int
    bankroll_id: str
    currency: str
    objective: EconomicObjective = (
        EconomicObjective.LONG_RUN_RISK_ADJUSTED_BANKROLL_GROWTH
    )

    max_stake_fraction: Decimal = Decimal("0.02")
    max_stake_amount: Decimal | None = None
    max_session_loss_fraction: Decimal = Decimal("0.05")
    max_day_loss_fraction: Decimal = Decimal("0.05")
    max_drawdown_fraction: Decimal = Decimal("0.20")
    max_capital_at_risk_fraction: Decimal = Decimal("0.20")
    max_event_concentration_fraction: Decimal = Decimal("1")
    max_market_concentration_fraction: Decimal = Decimal("1")
    max_provider_concentration_fraction: Decimal = Decimal("1")
    max_sport_concentration_fraction: Decimal = Decimal("1")
    max_turnover_fraction: Decimal = Decimal("1")
    max_risk_of_ruin: Decimal = Decimal("0.01")
    max_execution_slippage_fraction: Decimal = Decimal("0.01")
    max_quote_age_seconds: Decimal = Decimal("5")
    minimum_data_quality: Decimal = Decimal("0")

    max_concurrent_positions: int = 1
    max_parlay_legs: int = 1
    automation_level: AutomationLevel = AutomationLevel.ANALYSIS_ONLY
    emergency_stop: bool = False

    blocked_sports: frozenset[str] = frozenset()
    blocked_providers: frozenset[str] = frozenset()
    blocked_markets: frozenset[str] = frozenset()

    _authority_operations_sealed = False

    def __post_init__(
        self,
        _text_validator=_canonical_text,
        _positive_int_validator=_positive_int,
        _fraction_validator=_fraction,
        _optional_nonnegative_decimal_validator=_optional_nonnegative_decimal,
        _nonnegative_decimal_validator=_nonnegative_decimal,
        _nonnegative_int_validator=_nonnegative_int,
        _restrictions_validator=_canonical_restrictions,
        _objective_type=EconomicObjective,
        _automation_type=AutomationLevel,
        _error_type=EconomicGoalContractError,
    ) -> None:
        _text_validator("goal_id", self.goal_id)
        _positive_int_validator("revision", self.revision)
        _text_validator("bankroll_id", self.bankroll_id)

        currency = _text_validator("currency", self.currency)
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise _error_type(
                "currency must be a three-letter uppercase ASCII code"
            )
        if currency != currency.upper():
            raise _error_type(
                "currency must be a three-letter uppercase ASCII code"
            )

        if type(self.objective) is not _objective_type:
            raise _error_type("objective must be an EconomicObjective")

        _fraction_validator("max_stake_fraction", self.max_stake_fraction)
        _optional_nonnegative_decimal_validator("max_stake_amount", self.max_stake_amount)
        _fraction_validator("max_session_loss_fraction", self.max_session_loss_fraction)
        _fraction_validator("max_day_loss_fraction", self.max_day_loss_fraction)
        _fraction_validator("max_drawdown_fraction", self.max_drawdown_fraction)
        _fraction_validator(
            "max_capital_at_risk_fraction", self.max_capital_at_risk_fraction
        )
        _fraction_validator(
            "max_event_concentration_fraction", self.max_event_concentration_fraction
        )
        _fraction_validator(
            "max_market_concentration_fraction", self.max_market_concentration_fraction
        )
        _fraction_validator(
            "max_provider_concentration_fraction",
            self.max_provider_concentration_fraction,
        )
        _fraction_validator(
            "max_sport_concentration_fraction", self.max_sport_concentration_fraction
        )
        _nonnegative_decimal_validator("max_turnover_fraction", self.max_turnover_fraction)
        _fraction_validator("max_risk_of_ruin", self.max_risk_of_ruin)
        _fraction_validator(
            "max_execution_slippage_fraction",
            self.max_execution_slippage_fraction,
        )
        _nonnegative_decimal_validator("max_quote_age_seconds", self.max_quote_age_seconds)
        _fraction_validator("minimum_data_quality", self.minimum_data_quality)

        _nonnegative_int_validator(
            "max_concurrent_positions", self.max_concurrent_positions
        )
        _positive_int_validator("max_parlay_legs", self.max_parlay_legs)
        if type(self.automation_level) is not _automation_type:
            raise _error_type(
                "automation_level must be an AutomationLevel"
            )
        if type(self.emergency_stop) is not bool:
            raise _error_type("emergency_stop must be a bool")

        _restrictions_validator("blocked_sports", self.blocked_sports)
        _restrictions_validator("blocked_providers", self.blocked_providers)
        _restrictions_validator("blocked_markets", self.blocked_markets)

    def validate_automatic_successor(self, candidate: "EconomicGoalContract") -> None:
        """Validate a machine-proposed successor without authority expansion.

        This operation is intentionally asymmetric.  Automatic actors may lower
        ceilings, raise quality floors, add deny-list restrictions, reduce the
        autonomy level, or engage the emergency stop.  They may never perform
        the inverse transition.  Owner-authorized widening is deliberately not
        represented by this method and must cross a separate future authority
        boundary.
        """

        _CANONICAL_TRANSITION_VALIDATOR(self, candidate)

_CANONICAL_CONTRACT_TYPE: Final = EconomicGoalContract

_CONTRACT_FIELD_NAMES: Final = (
    "goal_id",
    "revision",
    "bankroll_id",
    "currency",
    "objective",
    "max_stake_fraction",
    "max_stake_amount",
    "max_session_loss_fraction",
    "max_day_loss_fraction",
    "max_drawdown_fraction",
    "max_capital_at_risk_fraction",
    "max_event_concentration_fraction",
    "max_market_concentration_fraction",
    "max_provider_concentration_fraction",
    "max_sport_concentration_fraction",
    "max_turnover_fraction",
    "max_risk_of_ruin",
    "max_execution_slippage_fraction",
    "max_quote_age_seconds",
    "minimum_data_quality",
    "max_concurrent_positions",
    "max_parlay_legs",
    "automation_level",
    "emergency_stop",
    "blocked_sports",
    "blocked_providers",
    "blocked_markets",
)

# Capture the original slot descriptors once so class-level rebinding cannot
# redirect economic-goal validation or identity reads to a forged descriptor.
_CANONICAL_CONTRACT_FIELD_GETTERS: Final = tuple(
    (name, EconomicGoalContract.__dict__[name].__get__)
    for name in _CONTRACT_FIELD_NAMES
)


def _canonical_contract_snapshot(
    contract: EconomicGoalContract,
    _field_getters=_CANONICAL_CONTRACT_FIELD_GETTERS,
    _contract_type=_CANONICAL_CONTRACT_TYPE,
) -> tuple[object, ...]:
    return tuple(
        getter(contract, _contract_type)
        for _, getter in _field_getters
    )


def _validate_contract_bound(
    self: EconomicGoalContract,
    _field_getters=_CANONICAL_CONTRACT_FIELD_GETTERS,
    _contract_type=_CANONICAL_CONTRACT_TYPE,
    _text_validator=_canonical_text,
    _positive_int_validator=_positive_int,
    _fraction_validator=_fraction,
    _optional_nonnegative_decimal_validator=_optional_nonnegative_decimal,
    _nonnegative_decimal_validator=_nonnegative_decimal,
    _nonnegative_int_validator=_nonnegative_int,
    _restrictions_validator=_canonical_restrictions,
    _snapshot=_canonical_contract_snapshot,
    _objective_type=EconomicObjective,
    _automation_type=AutomationLevel,
    _error_type=EconomicGoalContractError,
) -> None:
    """Validate contract fields through captured slot descriptors."""

    if type(self) is not _contract_type:
        raise _error_type("economic goal must use the exact contract type")
    values = _snapshot(self, _field_getters)
    (
        goal_id, revision, bankroll_id, currency, objective,
        max_stake_fraction, max_stake_amount, max_session_loss_fraction,
        max_day_loss_fraction, max_drawdown_fraction, max_capital_at_risk_fraction,
        max_event_concentration_fraction, max_market_concentration_fraction,
        max_provider_concentration_fraction, max_sport_concentration_fraction,
        max_turnover_fraction, max_risk_of_ruin, max_execution_slippage_fraction,
        max_quote_age_seconds, minimum_data_quality, max_concurrent_positions,
        max_parlay_legs, automation_level, emergency_stop,
        blocked_sports, blocked_providers, blocked_markets,
    ) = values

    _text_validator("goal_id", goal_id)
    _positive_int_validator("revision", revision)
    _text_validator("bankroll_id", bankroll_id)
    currency = _text_validator("currency", currency)
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise _error_type("currency must be a three-letter uppercase ASCII code")
    if currency != currency.upper():
        raise _error_type("currency must be a three-letter uppercase ASCII code")
    if type(objective) is not _objective_type:
        raise _error_type("objective must be an EconomicObjective")

    _fraction_validator("max_stake_fraction", max_stake_fraction)
    _optional_nonnegative_decimal_validator("max_stake_amount", max_stake_amount)
    _fraction_validator("max_session_loss_fraction", max_session_loss_fraction)
    _fraction_validator("max_day_loss_fraction", max_day_loss_fraction)
    _fraction_validator("max_drawdown_fraction", max_drawdown_fraction)
    _fraction_validator("max_capital_at_risk_fraction", max_capital_at_risk_fraction)
    _fraction_validator("max_event_concentration_fraction", max_event_concentration_fraction)
    _fraction_validator("max_market_concentration_fraction", max_market_concentration_fraction)
    _fraction_validator("max_provider_concentration_fraction", max_provider_concentration_fraction)
    _fraction_validator("max_sport_concentration_fraction", max_sport_concentration_fraction)
    _nonnegative_decimal_validator("max_turnover_fraction", max_turnover_fraction)
    _fraction_validator("max_risk_of_ruin", max_risk_of_ruin)
    _fraction_validator("max_execution_slippage_fraction", max_execution_slippage_fraction)
    _nonnegative_decimal_validator("max_quote_age_seconds", max_quote_age_seconds)
    _fraction_validator("minimum_data_quality", minimum_data_quality)

    _nonnegative_int_validator("max_concurrent_positions", max_concurrent_positions)
    _positive_int_validator("max_parlay_legs", max_parlay_legs)
    if type(automation_level) is not _automation_type:
        raise _error_type("automation_level must be an AutomationLevel")
    if type(emergency_stop) is not bool:
        raise _error_type("emergency_stop must be a bool")

    _restrictions_validator("blocked_sports", blocked_sports)
    _restrictions_validator("blocked_providers", blocked_providers)
    _restrictions_validator("blocked_markets", blocked_markets)


def _capture_callable_authority_graph(root):
    """Capture every callable reachable through function defaults, cycle-safely."""

    captured = []
    seen: set[int] = set()

    def visit(candidate) -> None:
        if not callable(candidate):
            return
        identity = id(candidate)
        if identity in seen:
            return
        seen.add(identity)
        defaults = getattr(candidate, "__defaults__", None)
        kwdefaults = getattr(candidate, "__kwdefaults__", None)
        kwdefault_items = tuple((kwdefaults or {}).items())
        captured.append(
            (
                candidate,
                getattr(candidate, "__code__", None),
                defaults,
                kwdefaults,
                kwdefault_items,
            )
        )
        for value in defaults or ():
            if callable(value):
                visit(value)
        for _, value in kwdefault_items:
            if callable(value):
                visit(value)

    visit(root)
    return tuple(captured)


def _make_contract_post_init_authority(operation):
    authority_graph = _capture_callable_authority_graph(operation)
    error_type = EconomicGoalContractError

    def require_authority() -> None:
        for index, (
            callable_object,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
        ) in enumerate(authority_graph):
            if getattr(callable_object, "__code__", None) is not expected_code:
                if index == 0:
                    raise error_type("economic-goal contract validator authority changed")
                raise error_type("economic-goal contract nested validator authority changed")
            if getattr(callable_object, "__defaults__", None) is not expected_defaults:
                if index == 0:
                    raise error_type("economic-goal contract validator defaults authority changed")
                raise error_type("economic-goal contract nested validator defaults authority changed")
            current_kwdefaults = getattr(callable_object, "__kwdefaults__", None)
            if (
                current_kwdefaults is not expected_kwdefaults
                or tuple((current_kwdefaults or {}).items()) != expected_kwdefault_items
            ):
                if index == 0:
                    raise error_type(
                        "economic-goal contract validator keyword defaults authority changed"
                    )
                raise error_type(
                    "economic-goal contract nested validator keyword defaults authority changed"
                )

    def bound(self: EconomicGoalContract) -> None:
        require_authority()
        operation(self)
        require_authority()

    return bound

_CANONICAL_CONTRACT_VALIDATOR: Final = _make_contract_post_init_authority(
    _validate_contract_bound
)
EconomicGoalContract.__post_init__ = _CANONICAL_CONTRACT_VALIDATOR


# The custom initializer writes all slots directly and then invokes the captured
# canonical validator, so constructor-time validation never dispatches through a
# mutable class-level __post_init__ alias.
def _contract_init_authority(
    self: EconomicGoalContract,
    goal_id: str,
    revision: int,
    bankroll_id: str,
    currency: str,
    objective: EconomicObjective = EconomicObjective.LONG_RUN_RISK_ADJUSTED_BANKROLL_GROWTH,
    max_stake_fraction: Decimal = Decimal("0.02"),
    max_stake_amount: Decimal | None = None,
    max_session_loss_fraction: Decimal = Decimal("0.05"),
    max_day_loss_fraction: Decimal = Decimal("0.05"),
    max_drawdown_fraction: Decimal = Decimal("0.20"),
    max_capital_at_risk_fraction: Decimal = Decimal("0.20"),
    max_event_concentration_fraction: Decimal = Decimal("1"),
    max_market_concentration_fraction: Decimal = Decimal("1"),
    max_provider_concentration_fraction: Decimal = Decimal("1"),
    max_sport_concentration_fraction: Decimal = Decimal("1"),
    max_turnover_fraction: Decimal = Decimal("1"),
    max_risk_of_ruin: Decimal = Decimal("0.01"),
    max_execution_slippage_fraction: Decimal = Decimal("0.01"),
    max_quote_age_seconds: Decimal = Decimal("5"),
    minimum_data_quality: Decimal = Decimal("0"),
    max_concurrent_positions: int = 1,
    max_parlay_legs: int = 1,
    automation_level: AutomationLevel = AutomationLevel.ANALYSIS_ONLY,
    emergency_stop: bool = False,
    blocked_sports: frozenset[str] = frozenset(),
    blocked_providers: frozenset[str] = frozenset(),
    blocked_markets: frozenset[str] = frozenset(),
    _validator=_CANONICAL_CONTRACT_VALIDATOR,
    _setattr=_CONTRACT_OBJECT_SETATTR,
) -> None:
    for name, value in (
        ("goal_id", goal_id),
        ("revision", revision),
        ("bankroll_id", bankroll_id),
        ("currency", currency),
        ("objective", objective),
        ("max_stake_fraction", max_stake_fraction),
        ("max_stake_amount", max_stake_amount),
        ("max_session_loss_fraction", max_session_loss_fraction),
        ("max_day_loss_fraction", max_day_loss_fraction),
        ("max_drawdown_fraction", max_drawdown_fraction),
        ("max_capital_at_risk_fraction", max_capital_at_risk_fraction),
        ("max_event_concentration_fraction", max_event_concentration_fraction),
        ("max_market_concentration_fraction", max_market_concentration_fraction),
        ("max_provider_concentration_fraction", max_provider_concentration_fraction),
        ("max_sport_concentration_fraction", max_sport_concentration_fraction),
        ("max_turnover_fraction", max_turnover_fraction),
        ("max_risk_of_ruin", max_risk_of_ruin),
        ("max_execution_slippage_fraction", max_execution_slippage_fraction),
        ("max_quote_age_seconds", max_quote_age_seconds),
        ("minimum_data_quality", minimum_data_quality),
        ("max_concurrent_positions", max_concurrent_positions),
        ("max_parlay_legs", max_parlay_legs),
        ("automation_level", automation_level),
        ("emergency_stop", emergency_stop),
        ("blocked_sports", blocked_sports),
        ("blocked_providers", blocked_providers),
        ("blocked_markets", blocked_markets),
    ):
        _setattr(self, name, value)
    _validator(self)


def _make_contract_constructor_authority(operation):
    authority_graph = _capture_callable_authority_graph(operation)
    error_type = EconomicGoalContractError

    def require_authority() -> None:
        for index, (
            callable_object,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
        ) in enumerate(authority_graph):
            if getattr(callable_object, "__code__", None) is not expected_code:
                if index == 0:
                    raise error_type("economic-goal constructor authority changed")
                raise error_type("economic-goal constructor nested authority changed")
            if getattr(callable_object, "__defaults__", None) is not expected_defaults:
                if index == 0:
                    raise error_type("economic-goal constructor defaults authority changed")
                raise error_type("economic-goal constructor nested defaults authority changed")
            current_kwdefaults = getattr(callable_object, "__kwdefaults__", None)
            if (
                current_kwdefaults is not expected_kwdefaults
                or tuple((current_kwdefaults or {}).items()) != expected_kwdefault_items
            ):
                if index == 0:
                    raise error_type(
                        "economic-goal constructor keyword defaults authority changed"
                    )
                raise error_type(
                    "economic-goal constructor nested keyword defaults authority changed"
                )

    def bound(*args, **kwargs):
        require_authority()
        result = operation(*args, **kwargs)
        require_authority()
        return result

    return bound

def _bind_contract_constructor(operation):
    bound_operation = _make_contract_constructor_authority(operation)

    def bound(
        self: EconomicGoalContract,
        goal_id: str,
        revision: int,
        bankroll_id: str,
        currency: str,
        objective: EconomicObjective = EconomicObjective.LONG_RUN_RISK_ADJUSTED_BANKROLL_GROWTH,
        max_stake_fraction: Decimal = Decimal("0.02"),
        max_stake_amount: Decimal | None = None,
        max_session_loss_fraction: Decimal = Decimal("0.05"),
        max_day_loss_fraction: Decimal = Decimal("0.05"),
        max_drawdown_fraction: Decimal = Decimal("0.20"),
        max_capital_at_risk_fraction: Decimal = Decimal("0.20"),
        max_event_concentration_fraction: Decimal = Decimal("1"),
        max_market_concentration_fraction: Decimal = Decimal("1"),
        max_provider_concentration_fraction: Decimal = Decimal("1"),
        max_sport_concentration_fraction: Decimal = Decimal("1"),
        max_turnover_fraction: Decimal = Decimal("1"),
        max_risk_of_ruin: Decimal = Decimal("0.01"),
        max_execution_slippage_fraction: Decimal = Decimal("0.01"),
        max_quote_age_seconds: Decimal = Decimal("5"),
        minimum_data_quality: Decimal = Decimal("0"),
        max_concurrent_positions: int = 1,
        max_parlay_legs: int = 1,
        automation_level: AutomationLevel = AutomationLevel.ANALYSIS_ONLY,
        emergency_stop: bool = False,
        blocked_sports: frozenset[str] = frozenset(),
        blocked_providers: frozenset[str] = frozenset(),
        blocked_markets: frozenset[str] = frozenset(),
    ) -> None:
        bound_operation(
            self,
            goal_id,
            revision,
            bankroll_id,
            currency,
            objective,
            max_stake_fraction,
            max_stake_amount,
            max_session_loss_fraction,
            max_day_loss_fraction,
            max_drawdown_fraction,
            max_capital_at_risk_fraction,
            max_event_concentration_fraction,
            max_market_concentration_fraction,
            max_provider_concentration_fraction,
            max_sport_concentration_fraction,
            max_turnover_fraction,
            max_risk_of_ruin,
            max_execution_slippage_fraction,
            max_quote_age_seconds,
            minimum_data_quality,
            max_concurrent_positions,
            max_parlay_legs,
            automation_level,
            emergency_stop,
            blocked_sports,
            blocked_providers,
            blocked_markets,
        )

    return bound


_CANONICAL_CONTRACT_INIT: Final = _bind_contract_constructor(
    _contract_init_authority
)


def _snapshot_transition_contract(
    contract: EconomicGoalContract,
    _contract_type=_CANONICAL_CONTRACT_TYPE,
    _contract_validator=_CANONICAL_CONTRACT_VALIDATOR,
    _object_new=object.__new__,
    _object_setattr=object.__setattr__,
    _error_type=EconomicGoalContractError,
    _snapshot=_canonical_contract_snapshot,
    _field_names=_CONTRACT_FIELD_NAMES,
) -> EconomicGoalContract:
    if type(contract) is not _contract_type:
        raise _error_type(
            "automatic transition requires EconomicGoalContract instances"
        )
    snapshot = _object_new(_contract_type)
    values = _snapshot(contract)
    for name, value in zip(_field_names, values):
        _object_setattr(snapshot, name, value)
    _contract_validator(snapshot)
    return snapshot


def _require_same(
    name: str,
    previous: object,
    candidate: object,
    _error_type=EconomicGoalContractError,
) -> None:
    if candidate != previous:
        raise _error_type(
            f"automatic transition must preserve {name}"
        )


def _require_cap_not_increased(
    name: str,
    previous: Decimal,
    candidate: Decimal,
    _error_type=EconomicGoalContractError,
) -> None:
    if candidate > previous:
        raise _error_type(
            f"automatic transition must not increase {name}"
        )


def _require_optional_cap_not_increased(
    name: str,
    previous: Decimal | None,
    candidate: Decimal | None,
    _error_type=EconomicGoalContractError,
) -> None:
    if previous is None:
        # Moving from no contract-level absolute cap to a finite one is tighter.
        return
    if candidate is None or candidate > previous:
        raise _error_type(
            f"automatic transition must not increase or remove {name}"
        )


def _require_floor_not_decreased(
    name: str,
    previous: Decimal,
    candidate: Decimal,
    _error_type=EconomicGoalContractError,
) -> None:
    if candidate < previous:
        raise _error_type(
            f"automatic transition must not decrease {name}"
        )


def _require_int_cap_not_increased(
    name: str,
    previous: int,
    candidate: int,
    _error_type=EconomicGoalContractError,
) -> None:
    if candidate > previous:
        raise _error_type(
            f"automatic transition must not increase {name}"
        )


def _require_restrictions_not_removed(
    name: str,
    previous: frozenset[str],
    candidate: frozenset[str],
    _error_type=EconomicGoalContractError,
) -> None:
    if not previous.issubset(candidate):
        raise _error_type(
            f"automatic transition must not remove {name} restrictions"
        )


def _validate_automatic_transition_bound(
    previous: EconomicGoalContract,
    candidate: EconomicGoalContract,
    _contract_type=_CANONICAL_CONTRACT_TYPE,
    _contract_validator=_CANONICAL_CONTRACT_VALIDATOR,
    _same_guard=_require_same,
    _cap_guard=_require_cap_not_increased,
    _optional_cap_guard=_require_optional_cap_not_increased,
    _floor_guard=_require_floor_not_decreased,
    _int_cap_guard=_require_int_cap_not_increased,
    _restrictions_guard=_require_restrictions_not_removed,
    _snapshot=_canonical_contract_snapshot,
    _field_names=_CONTRACT_FIELD_NAMES,
    _error_type=EconomicGoalContractError,
) -> None:
    """Prove that ``candidate`` does not enlarge ``previous`` authority.

    Both arguments must already be valid typed contracts.  The successor must be
    the immediately next revision of the same goal/bankroll/currency/objective.
    Equality of authority limits is accepted; this validator establishes
    *non-expansion*, not that every revision necessarily tightens a limit.
    """

    if (
        type(previous) is not _contract_type
        or type(candidate) is not _contract_type
    ):
        raise _error_type("automatic transition requires EconomicGoalContract instances")

    _contract_validator(previous)
    _contract_validator(candidate)
    previous_before = _snapshot(previous)
    candidate_before = _snapshot(candidate)
    _contract_validator(previous)
    _contract_validator(candidate)
    previous_after = _snapshot(previous)
    candidate_after = _snapshot(candidate)
    if previous_before != previous_after or candidate_before != candidate_after:
        raise _error_type("economic goal changed during automatic transition validation")

    previous_view = dict(zip(_field_names, previous_after))
    candidate_view = dict(zip(_field_names, candidate_after))

    _same_guard("goal_id", previous_view["goal_id"], candidate_view["goal_id"])
    _same_guard("bankroll_id", previous_view["bankroll_id"], candidate_view["bankroll_id"])
    _same_guard("currency", previous_view["currency"], candidate_view["currency"])
    _same_guard("objective", previous_view["objective"], candidate_view["objective"])

    if candidate_view["revision"] != previous_view["revision"] + 1:
        raise _error_type("automatic transition must advance revision by exactly one")

    _cap_guard("max_stake_fraction", previous_view["max_stake_fraction"], candidate_view["max_stake_fraction"])
    _optional_cap_guard("max_stake_amount", previous_view["max_stake_amount"], candidate_view["max_stake_amount"])
    _cap_guard("max_session_loss_fraction", previous_view["max_session_loss_fraction"], candidate_view["max_session_loss_fraction"])
    _cap_guard("max_day_loss_fraction", previous_view["max_day_loss_fraction"], candidate_view["max_day_loss_fraction"])
    _cap_guard("max_drawdown_fraction", previous_view["max_drawdown_fraction"], candidate_view["max_drawdown_fraction"])
    _cap_guard("max_capital_at_risk_fraction", previous_view["max_capital_at_risk_fraction"], candidate_view["max_capital_at_risk_fraction"])
    _cap_guard("max_event_concentration_fraction", previous_view["max_event_concentration_fraction"], candidate_view["max_event_concentration_fraction"])
    _cap_guard("max_market_concentration_fraction", previous_view["max_market_concentration_fraction"], candidate_view["max_market_concentration_fraction"])
    _cap_guard("max_provider_concentration_fraction", previous_view["max_provider_concentration_fraction"], candidate_view["max_provider_concentration_fraction"])
    _cap_guard("max_sport_concentration_fraction", previous_view["max_sport_concentration_fraction"], candidate_view["max_sport_concentration_fraction"])
    _cap_guard("max_turnover_fraction", previous_view["max_turnover_fraction"], candidate_view["max_turnover_fraction"])
    _cap_guard("max_risk_of_ruin", previous_view["max_risk_of_ruin"], candidate_view["max_risk_of_ruin"])
    _cap_guard("max_execution_slippage_fraction", previous_view["max_execution_slippage_fraction"], candidate_view["max_execution_slippage_fraction"])
    _cap_guard("max_quote_age_seconds", previous_view["max_quote_age_seconds"], candidate_view["max_quote_age_seconds"])
    _floor_guard("minimum_data_quality", previous_view["minimum_data_quality"], candidate_view["minimum_data_quality"])

    _int_cap_guard("max_concurrent_positions", previous_view["max_concurrent_positions"], candidate_view["max_concurrent_positions"])
    _int_cap_guard("max_parlay_legs", previous_view["max_parlay_legs"], candidate_view["max_parlay_legs"])
    if candidate_view["automation_level"] > previous_view["automation_level"]:
        raise _error_type("automatic transition must not increase automation_level")
    if previous_view["emergency_stop"] and not candidate_view["emergency_stop"]:
        raise _error_type("automatic transition must not clear emergency_stop")

    _restrictions_guard("blocked_sports", previous_view["blocked_sports"], candidate_view["blocked_sports"])
    _restrictions_guard("blocked_providers", previous_view["blocked_providers"], candidate_view["blocked_providers"])
    _restrictions_guard("blocked_markets", previous_view["blocked_markets"], candidate_view["blocked_markets"])


def _make_transition_validator_authority(operation):
    authority_graph = _capture_callable_authority_graph(operation)

    def require_authority() -> None:
        for index, (
            callable_object,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
        ) in enumerate(authority_graph):
            if getattr(callable_object, "__code__", None) is not expected_code:
                if index == 0:
                    raise EconomicGoalContractError(
                        "automatic transition validator authority changed"
                    )
                raise EconomicGoalContractError(
                    "automatic transition nested validator authority changed"
                )
            if getattr(callable_object, "__defaults__", None) is not expected_defaults:
                if index == 0:
                    raise EconomicGoalContractError(
                        "automatic transition validator defaults authority changed"
                    )
                raise EconomicGoalContractError(
                    "automatic transition nested validator defaults authority changed"
                )
            current_kwdefaults = getattr(callable_object, "__kwdefaults__", None)
            if (
                current_kwdefaults is not expected_kwdefaults
                or tuple((current_kwdefaults or {}).items()) != expected_kwdefault_items
            ):
                if index == 0:
                    raise EconomicGoalContractError(
                        "automatic transition validator keyword defaults authority changed"
                    )
                raise EconomicGoalContractError(
                    "automatic transition nested validator keyword defaults authority changed"
                )

    def bound(
        previous: EconomicGoalContract,
        candidate: EconomicGoalContract,
    ) -> None:
        require_authority()
        operation(previous, candidate)
        require_authority()

    return bound

_CANONICAL_TRANSITION_VALIDATOR: Final = _make_transition_validator_authority(
    _validate_automatic_transition_bound
)

# Bind the convenience method to the canonical transition function object after
# its definition.  This avoids resolving a mutable module alias when an owner
# contract validates a machine-proposed successor.
def _validate_automatic_successor_bound(
    self: EconomicGoalContract,
    candidate: EconomicGoalContract,
    _validator=_CANONICAL_TRANSITION_VALIDATOR,
) -> None:
    _validator(self, candidate)


def _bind_contract_successor_operation(operation):
    def bound(
        self: EconomicGoalContract,
        candidate: EconomicGoalContract,
    ) -> None:
        operation(self, candidate)

    return bound


EconomicGoalContract.validate_automatic_successor = _bind_contract_successor_operation(
    _CANONICAL_TRANSITION_VALIDATOR
)

# Freeze the public method once its closure has captured the canonical validator.
EconomicGoalContract._authority_operations_sealed = True

for _sealed_contract_name in _EconomicGoalContractMeta._AUTHORITY_NAMES:
    setattr(
        _EconomicGoalContractMeta,
        _sealed_contract_name,
        _build_contract_class_guard(_sealed_contract_name),
    )
del _sealed_contract_name


# Keep the public transition proof noninjectable while capturing the canonical
# implementation object against later module rebinding.
def _bind_transition_operation(operation):
    def bound(
        previous: EconomicGoalContract,
        candidate: EconomicGoalContract,
    ) -> None:
        operation(previous, candidate)

    return bound


validate_automatic_transition = _bind_transition_operation(
    _CANONICAL_TRANSITION_VALIDATOR
)
