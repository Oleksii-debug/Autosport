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


def _canonical_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise EconomicGoalContractError(f"{name} must be a string")
    if not value or value != value.strip():
        raise EconomicGoalContractError(
            f"{name} must be a non-empty canonical string"
        )
    if "\x00" in value:
        raise EconomicGoalContractError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise EconomicGoalContractError(f"{name} must be valid UTF-8 text") from exc
    return value


def _decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise EconomicGoalContractError(f"{name} must be an exact Decimal")
    if not value.is_finite():
        raise EconomicGoalContractError(f"{name} must be finite")
    return value


def _fraction(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result < _ZERO or result > _ONE:
        raise EconomicGoalContractError(f"{name} must be between 0 and 1 inclusive")
    return result


def _nonnegative_decimal(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result < _ZERO:
        raise EconomicGoalContractError(f"{name} must be non-negative")
    return result


def _optional_nonnegative_decimal(name: str, value: object) -> Decimal | None:
    if value is None:
        return None
    return _nonnegative_decimal(name, value)


def _nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EconomicGoalContractError(f"{name} must be a non-boolean integer")
    if value < 0:
        raise EconomicGoalContractError(f"{name} must be non-negative")
    return value


def _positive_int(name: str, value: object) -> int:
    result = _nonnegative_int(name, value)
    if result == 0:
        raise EconomicGoalContractError(f"{name} must be positive")
    return result


def _canonical_restrictions(name: str, value: object) -> frozenset[str]:
    if not isinstance(value, frozenset):
        raise EconomicGoalContractError(f"{name} must be a frozenset of strings")
    normalized: set[str] = set()
    for item in value:
        normalized.add(_canonical_text(f"{name} member", item))
    if len(normalized) != len(value):
        # Defensive only; frozenset already removes exact duplicates.  Keep the
        # invariant explicit if its input contract ever changes.
        raise EconomicGoalContractError(f"{name} must contain unique members")
    return value


@dataclass(frozen=True, slots=True)
class EconomicGoalContract:
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

    def __post_init__(self) -> None:
        _canonical_text("goal_id", self.goal_id)
        _positive_int("revision", self.revision)
        _canonical_text("bankroll_id", self.bankroll_id)

        currency = _canonical_text("currency", self.currency)
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise EconomicGoalContractError(
                "currency must be a three-letter uppercase ASCII code"
            )
        if currency != currency.upper():
            raise EconomicGoalContractError(
                "currency must be a three-letter uppercase ASCII code"
            )

        if not isinstance(self.objective, EconomicObjective):
            raise EconomicGoalContractError("objective must be an EconomicObjective")

        _fraction("max_stake_fraction", self.max_stake_fraction)
        _optional_nonnegative_decimal("max_stake_amount", self.max_stake_amount)
        _fraction("max_session_loss_fraction", self.max_session_loss_fraction)
        _fraction("max_day_loss_fraction", self.max_day_loss_fraction)
        _fraction("max_drawdown_fraction", self.max_drawdown_fraction)
        _fraction(
            "max_capital_at_risk_fraction", self.max_capital_at_risk_fraction
        )
        _fraction(
            "max_event_concentration_fraction", self.max_event_concentration_fraction
        )
        _fraction(
            "max_market_concentration_fraction", self.max_market_concentration_fraction
        )
        _fraction(
            "max_provider_concentration_fraction",
            self.max_provider_concentration_fraction,
        )
        _fraction(
            "max_sport_concentration_fraction", self.max_sport_concentration_fraction
        )
        _nonnegative_decimal("max_turnover_fraction", self.max_turnover_fraction)
        _fraction("max_risk_of_ruin", self.max_risk_of_ruin)
        _fraction(
            "max_execution_slippage_fraction",
            self.max_execution_slippage_fraction,
        )
        _nonnegative_decimal("max_quote_age_seconds", self.max_quote_age_seconds)
        _fraction("minimum_data_quality", self.minimum_data_quality)

        _nonnegative_int("max_concurrent_positions", self.max_concurrent_positions)
        _positive_int("max_parlay_legs", self.max_parlay_legs)
        if not isinstance(self.automation_level, AutomationLevel):
            raise EconomicGoalContractError(
                "automation_level must be an AutomationLevel"
            )
        if not isinstance(self.emergency_stop, bool):
            raise EconomicGoalContractError("emergency_stop must be a bool")

        _canonical_restrictions("blocked_sports", self.blocked_sports)
        _canonical_restrictions("blocked_providers", self.blocked_providers)
        _canonical_restrictions("blocked_markets", self.blocked_markets)

    def validate_automatic_successor(self, candidate: "EconomicGoalContract") -> None:
        """Validate a machine-proposed successor without authority expansion.

        This operation is intentionally asymmetric.  Automatic actors may lower
        ceilings, raise quality floors, add deny-list restrictions, reduce the
        autonomy level, or engage the emergency stop.  They may never perform
        the inverse transition.  Owner-authorized widening is deliberately not
        represented by this method and must cross a separate future authority
        boundary.
        """

        validate_automatic_transition(self, candidate)


def _require_same(name: str, previous: object, candidate: object) -> None:
    if candidate != previous:
        raise EconomicGoalContractError(
            f"automatic transition must preserve {name}"
        )


def _require_cap_not_increased(name: str, previous: Decimal, candidate: Decimal) -> None:
    if candidate > previous:
        raise EconomicGoalContractError(
            f"automatic transition must not increase {name}"
        )


def _require_optional_cap_not_increased(
    name: str, previous: Decimal | None, candidate: Decimal | None
) -> None:
    if previous is None:
        # Moving from no contract-level absolute cap to a finite one is tighter.
        return
    if candidate is None or candidate > previous:
        raise EconomicGoalContractError(
            f"automatic transition must not increase or remove {name}"
        )


def _require_floor_not_decreased(
    name: str, previous: Decimal, candidate: Decimal
) -> None:
    if candidate < previous:
        raise EconomicGoalContractError(
            f"automatic transition must not decrease {name}"
        )


def _require_int_cap_not_increased(name: str, previous: int, candidate: int) -> None:
    if candidate > previous:
        raise EconomicGoalContractError(
            f"automatic transition must not increase {name}"
        )


def _require_restrictions_not_removed(
    name: str, previous: frozenset[str], candidate: frozenset[str]
) -> None:
    if not previous.issubset(candidate):
        raise EconomicGoalContractError(
            f"automatic transition must not remove {name} restrictions"
        )


def validate_automatic_transition(
    previous: EconomicGoalContract,
    candidate: EconomicGoalContract,
) -> None:
    """Prove that ``candidate`` does not enlarge ``previous`` authority.

    Both arguments must already be valid typed contracts.  The successor must be
    the immediately next revision of the same goal/bankroll/currency/objective.
    Equality of authority limits is accepted; this validator establishes
    *non-expansion*, not that every revision necessarily tightens a limit.
    """

    if not isinstance(previous, EconomicGoalContract) or not isinstance(
        candidate, EconomicGoalContract
    ):
        raise EconomicGoalContractError(
            "automatic transition requires EconomicGoalContract instances"
        )

    _require_same("goal_id", previous.goal_id, candidate.goal_id)
    _require_same("bankroll_id", previous.bankroll_id, candidate.bankroll_id)
    _require_same("currency", previous.currency, candidate.currency)
    _require_same("objective", previous.objective, candidate.objective)

    if candidate.revision != previous.revision + 1:
        raise EconomicGoalContractError(
            "automatic transition must advance revision by exactly one"
        )

    _require_cap_not_increased(
        "max_stake_fraction",
        previous.max_stake_fraction,
        candidate.max_stake_fraction,
    )
    _require_optional_cap_not_increased(
        "max_stake_amount", previous.max_stake_amount, candidate.max_stake_amount
    )
    _require_cap_not_increased(
        "max_session_loss_fraction",
        previous.max_session_loss_fraction,
        candidate.max_session_loss_fraction,
    )
    _require_cap_not_increased(
        "max_day_loss_fraction",
        previous.max_day_loss_fraction,
        candidate.max_day_loss_fraction,
    )
    _require_cap_not_increased(
        "max_drawdown_fraction",
        previous.max_drawdown_fraction,
        candidate.max_drawdown_fraction,
    )
    _require_cap_not_increased(
        "max_capital_at_risk_fraction",
        previous.max_capital_at_risk_fraction,
        candidate.max_capital_at_risk_fraction,
    )
    _require_cap_not_increased(
        "max_event_concentration_fraction",
        previous.max_event_concentration_fraction,
        candidate.max_event_concentration_fraction,
    )
    _require_cap_not_increased(
        "max_market_concentration_fraction",
        previous.max_market_concentration_fraction,
        candidate.max_market_concentration_fraction,
    )
    _require_cap_not_increased(
        "max_provider_concentration_fraction",
        previous.max_provider_concentration_fraction,
        candidate.max_provider_concentration_fraction,
    )
    _require_cap_not_increased(
        "max_sport_concentration_fraction",
        previous.max_sport_concentration_fraction,
        candidate.max_sport_concentration_fraction,
    )
    _require_cap_not_increased(
        "max_turnover_fraction",
        previous.max_turnover_fraction,
        candidate.max_turnover_fraction,
    )
    _require_cap_not_increased(
        "max_risk_of_ruin", previous.max_risk_of_ruin, candidate.max_risk_of_ruin
    )
    _require_cap_not_increased(
        "max_execution_slippage_fraction",
        previous.max_execution_slippage_fraction,
        candidate.max_execution_slippage_fraction,
    )
    _require_cap_not_increased(
        "max_quote_age_seconds",
        previous.max_quote_age_seconds,
        candidate.max_quote_age_seconds,
    )
    _require_floor_not_decreased(
        "minimum_data_quality",
        previous.minimum_data_quality,
        candidate.minimum_data_quality,
    )

    _require_int_cap_not_increased(
        "max_concurrent_positions",
        previous.max_concurrent_positions,
        candidate.max_concurrent_positions,
    )
    _require_int_cap_not_increased(
        "max_parlay_legs", previous.max_parlay_legs, candidate.max_parlay_legs
    )
    if candidate.automation_level > previous.automation_level:
        raise EconomicGoalContractError(
            "automatic transition must not increase automation_level"
        )
    if previous.emergency_stop and not candidate.emergency_stop:
        raise EconomicGoalContractError(
            "automatic transition must not clear emergency_stop"
        )

    _require_restrictions_not_removed(
        "blocked_sports", previous.blocked_sports, candidate.blocked_sports
    )
    _require_restrictions_not_removed(
        "blocked_providers", previous.blocked_providers, candidate.blocked_providers
    )
    _require_restrictions_not_removed(
        "blocked_markets", previous.blocked_markets, candidate.blocked_markets
    )
