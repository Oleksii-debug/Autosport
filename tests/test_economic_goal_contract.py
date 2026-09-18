from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from autosport.economic_goal import (
    AutomationLevel,
    EconomicGoalContract,
    EconomicGoalContractError,
    EconomicObjective,
    validate_automatic_transition,
)


def _goal(**changes: object) -> EconomicGoalContract:
    values: dict[str, object] = {
        "goal_id": "owner-goal-v1",
        "revision": 1,
        "bankroll_id": "paper-main",
        "currency": "EUR",
        "objective": EconomicObjective.LONG_RUN_RISK_ADJUSTED_BANKROLL_GROWTH,
        "max_stake_fraction": Decimal("0.02"),
        "max_stake_amount": None,
        "max_session_loss_fraction": Decimal("0.05"),
        "max_day_loss_fraction": Decimal("0.05"),
        "max_drawdown_fraction": Decimal("0.20"),
        "max_capital_at_risk_fraction": Decimal("0.20"),
        "max_event_concentration_fraction": Decimal("0.50"),
        "max_market_concentration_fraction": Decimal("0.50"),
        "max_provider_concentration_fraction": Decimal("0.50"),
        "max_sport_concentration_fraction": Decimal("0.50"),
        "max_turnover_fraction": Decimal("1.5"),
        "max_risk_of_ruin": Decimal("0.01"),
        "max_execution_slippage_fraction": Decimal("0.01"),
        "max_quote_age_seconds": Decimal("5"),
        "minimum_data_quality": Decimal("0.70"),
        "max_concurrent_positions": 5,
        "max_parlay_legs": 4,
        "automation_level": AutomationLevel.SUPERVISED_EXECUTION,
        "emergency_stop": False,
        "blocked_sports": frozenset({"greyhound"}),
        "blocked_providers": frozenset({"provider:test"}),
        "blocked_markets": frozenset({"market:unsupported"}),
    }
    values.update(changes)
    return EconomicGoalContract(**values)  # type: ignore[arg-type]


def test_contract_is_immutable_and_accepts_unicode_identity() -> None:
    goal = _goal(goal_id="мета-власника", bankroll_id="банкрол-1")

    assert goal.currency == "EUR"
    assert hash(goal)
    with pytest.raises(FrozenInstanceError):
        goal.currency = "USD"  # type: ignore[misc]


def test_automation_levels_match_canonical_execution_contract() -> None:
    assert [
        (AutomationLevel.ANALYSIS_ONLY.name, int(AutomationLevel.ANALYSIS_ONLY)),
        (AutomationLevel.RECOMMENDATION.name, int(AutomationLevel.RECOMMENDATION)),
        (
            AutomationLevel.SUPERVISED_EXECUTION.name,
            int(AutomationLevel.SUPERVISED_EXECUTION),
        ),
        (AutomationLevel.BOUNDED_AUTONOMY.name, int(AutomationLevel.BOUNDED_AUTONOMY)),
        (AutomationLevel.HIGHER_AUTONOMY.name, int(AutomationLevel.HIGHER_AUTONOMY)),
    ] == [
        ("ANALYSIS_ONLY", 0),
        ("RECOMMENDATION", 1),
        ("SUPERVISED_EXECUTION", 2),
        ("BOUNDED_AUTONOMY", 3),
        ("HIGHER_AUTONOMY", 4),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("goal_id", " padded "),
        ("bankroll_id", ""),
        ("currency", "eur"),
        ("currency", "EURO"),
        ("revision", True),
        ("revision", 0),
        ("max_stake_fraction", 0.02),
        ("max_stake_fraction", Decimal("NaN")),
        ("max_stake_fraction", Decimal("1.01")),
        ("max_stake_amount", Decimal("-0.01")),
        ("max_event_concentration_fraction", Decimal("1.01")),
        ("max_turnover_fraction", Decimal("-0.01")),
        ("max_quote_age_seconds", Decimal("Infinity")),
        ("minimum_data_quality", Decimal("-0.01")),
        ("max_concurrent_positions", True),
        ("max_concurrent_positions", -1),
        ("max_parlay_legs", 0),
        ("automation_level", 2),
        ("emergency_stop", 1),
        ("blocked_sports", {"football"}),
        ("blocked_providers", frozenset({" padded "})),
    ],
)
def test_contract_rejects_noncanonical_authority_inputs(
    field: str, value: object
) -> None:
    with pytest.raises(EconomicGoalContractError):
        _goal(**{field: value})


def test_automatic_transition_accepts_only_safety_non_expansion() -> None:
    previous = _goal()
    candidate = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.01"),
        max_stake_amount=Decimal("25"),
        max_session_loss_fraction=Decimal("0.04"),
        max_day_loss_fraction=Decimal("0.04"),
        max_drawdown_fraction=Decimal("0.15"),
        max_capital_at_risk_fraction=Decimal("0.10"),
        max_event_concentration_fraction=Decimal("0.40"),
        max_market_concentration_fraction=Decimal("0.40"),
        max_provider_concentration_fraction=Decimal("0.40"),
        max_sport_concentration_fraction=Decimal("0.40"),
        max_turnover_fraction=Decimal("1.25"),
        max_risk_of_ruin=Decimal("0.005"),
        max_execution_slippage_fraction=Decimal("0.005"),
        max_quote_age_seconds=Decimal("3"),
        minimum_data_quality=Decimal("0.85"),
        max_concurrent_positions=3,
        max_parlay_legs=2,
        automation_level=AutomationLevel.RECOMMENDATION,
        emergency_stop=True,
        blocked_sports=previous.blocked_sports | {"boxing"},
        blocked_providers=previous.blocked_providers | {"provider:blocked"},
        blocked_markets=previous.blocked_markets | {"market:blocked"},
    )

    validate_automatic_transition(previous, candidate)
    previous.validate_automatic_successor(candidate)


def test_equal_authority_is_valid_non_expanding_successor() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2)

    validate_automatic_transition(previous, candidate)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_stake_fraction", Decimal("0.03")),
        ("max_session_loss_fraction", Decimal("0.06")),
        ("max_day_loss_fraction", Decimal("0.06")),
        ("max_drawdown_fraction", Decimal("0.21")),
        ("max_capital_at_risk_fraction", Decimal("0.21")),
        ("max_event_concentration_fraction", Decimal("0.51")),
        ("max_market_concentration_fraction", Decimal("0.51")),
        ("max_provider_concentration_fraction", Decimal("0.51")),
        ("max_sport_concentration_fraction", Decimal("0.51")),
        ("max_turnover_fraction", Decimal("1.51")),
        ("max_risk_of_ruin", Decimal("0.02")),
        ("max_execution_slippage_fraction", Decimal("0.02")),
        ("max_quote_age_seconds", Decimal("6")),
        ("minimum_data_quality", Decimal("0.69")),
        ("max_concurrent_positions", 6),
        ("max_parlay_legs", 5),
        ("automation_level", AutomationLevel.BOUNDED_AUTONOMY),
    ],
)
def test_automatic_transition_rejects_authority_expansion(
    field: str, value: object
) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, **{field: value})

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, candidate)


def test_automatic_transition_rejects_supervised_to_bounded_execution() -> None:
    previous = _goal(automation_level=AutomationLevel.SUPERVISED_EXECUTION)
    candidate = replace(
        previous,
        revision=2,
        automation_level=AutomationLevel.BOUNDED_AUTONOMY,
    )

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, candidate)


def test_automatic_transition_rejects_absolute_cap_removal_or_increase() -> None:
    previous = _goal(max_stake_amount=Decimal("20"))

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(
            previous, replace(previous, revision=2, max_stake_amount=None)
        )
    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(
            previous,
            replace(previous, revision=2, max_stake_amount=Decimal("20.01")),
        )


def test_automatic_transition_rejects_emergency_stop_clear() -> None:
    previous = _goal(emergency_stop=True)
    candidate = replace(previous, revision=2, emergency_stop=False)

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, candidate)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("blocked_sports", frozenset()),
        ("blocked_providers", frozenset()),
        ("blocked_markets", frozenset()),
    ],
)
def test_automatic_transition_rejects_removing_owner_restrictions(
    field: str, value: frozenset[str]
) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, **{field: value})

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, candidate)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("goal_id", "other-goal"),
        ("bankroll_id", "other-bankroll"),
        ("currency", "USD"),
    ],
)
def test_automatic_transition_rejects_identity_rebinding(
    field: str, value: object
) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, **{field: value})

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, candidate)


def test_automatic_transition_requires_exact_next_revision() -> None:
    previous = _goal()

    for revision in (1, 3):
        with pytest.raises(EconomicGoalContractError):
            validate_automatic_transition(previous, replace(previous, revision=revision))


def test_automatic_transition_requires_typed_contracts() -> None:
    previous = _goal()

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, object())  # type: ignore[arg-type]
