from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

import autosport.economic_goal as economic_goal_module

from autosport.economic_goal import (
    AutomationLevel,
    EconomicGoalContract,
    EconomicGoalContractError,
    EconomicObjective,
    validate_automatic_transition,
)


class _DecimalSubclass(Decimal):
    pass


class _StringSubclass(str):
    pass


class _IntSubclass(int):
    pass


class _FrozenSetSubclass(frozenset[str]):
    pass


class _GoalSubclass(EconomicGoalContract):
    pass


class _HostileComparable:
    comparisons = 0

    def __gt__(self, other: object) -> bool:
        type(self).comparisons += 1
        return False

    def __lt__(self, other: object) -> bool:
        type(self).comparisons += 1
        return False


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


def test_contract_rejects_oversize_authority_identity_text() -> None:
    with pytest.raises(EconomicGoalContractError, match="text size limit"):
        _goal(goal_id="g" * 513)


def test_contract_rejects_unbounded_restriction_cardinality() -> None:
    with pytest.raises(EconomicGoalContractError, match="restriction-count limit"):
        _goal(blocked_sports=frozenset(f"sport:{index}" for index in range(1025)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_stake_fraction", _DecimalSubclass("0.02")),
        ("max_stake_amount", _DecimalSubclass("10")),
        ("max_turnover_fraction", _DecimalSubclass("1.5")),
        ("max_quote_age_seconds", _DecimalSubclass("5")),
        ("goal_id", _StringSubclass("owner-goal-v1")),
        ("revision", _IntSubclass(1)),
        ("max_concurrent_positions", _IntSubclass(5)),
        ("blocked_sports", _FrozenSetSubclass({"greyhound"})),
    ],
)
def test_contract_rejects_authority_value_subclasses(
    field: str,
    value: object,
) -> None:
    with pytest.raises(EconomicGoalContractError):
        _goal(**{field: value})


def test_automatic_transition_rejects_contract_subclasses() -> None:
    previous = _goal()
    derived = _GoalSubclass(
        goal_id=previous.goal_id,
        revision=2,
        bankroll_id=previous.bankroll_id,
        currency=previous.currency,
        objective=previous.objective,
        max_stake_fraction=previous.max_stake_fraction,
        max_stake_amount=previous.max_stake_amount,
        max_session_loss_fraction=previous.max_session_loss_fraction,
        max_day_loss_fraction=previous.max_day_loss_fraction,
        max_drawdown_fraction=previous.max_drawdown_fraction,
        max_capital_at_risk_fraction=previous.max_capital_at_risk_fraction,
        max_event_concentration_fraction=previous.max_event_concentration_fraction,
        max_market_concentration_fraction=previous.max_market_concentration_fraction,
        max_provider_concentration_fraction=(
            previous.max_provider_concentration_fraction
        ),
        max_sport_concentration_fraction=previous.max_sport_concentration_fraction,
        max_turnover_fraction=previous.max_turnover_fraction,
        max_risk_of_ruin=previous.max_risk_of_ruin,
        max_execution_slippage_fraction=previous.max_execution_slippage_fraction,
        max_quote_age_seconds=previous.max_quote_age_seconds,
        minimum_data_quality=previous.minimum_data_quality,
        max_concurrent_positions=previous.max_concurrent_positions,
        max_parlay_legs=previous.max_parlay_legs,
        automation_level=previous.automation_level,
        emergency_stop=previous.emergency_stop,
        blocked_sports=previous.blocked_sports,
        blocked_providers=previous.blocked_providers,
        blocked_markets=previous.blocked_markets,
    )

    with pytest.raises(
        EconomicGoalContractError,
        match="requires EconomicGoalContract instances",
    ):
        validate_automatic_transition(previous, derived)



def test_contract_revalidation_ignores_rebound_legacy_helper_exports(
    monkeypatch,
) -> None:
    goal = _goal()
    object.__setattr__(goal, "max_stake_fraction", Decimal("9"))

    for helper_name in (
        "_canonical_text",
        "_decimal",
        "_fraction",
        "_nonnegative_decimal",
        "_optional_nonnegative_decimal",
        "_nonnegative_int",
        "_positive_int",
        "_canonical_restrictions",
    ):
        monkeypatch.setattr(
            economic_goal_module,
            helper_name,
            lambda *args, **kwargs: None,
        )

    with pytest.raises(EconomicGoalContractError):
        EconomicGoalContract.__post_init__(goal)


def test_contract_revalidation_ignores_decimal_module_rebinding(
    monkeypatch,
) -> None:
    goal = _goal()
    monkeypatch.setattr(economic_goal_module, "Decimal", _DecimalSubclass)
    object.__setattr__(goal, "max_stake_fraction", _DecimalSubclass("0.01"))

    with pytest.raises(EconomicGoalContractError, match="exact Decimal"):
        EconomicGoalContract.__post_init__(goal)


def test_transition_authority_ignores_rebound_public_exports(
    monkeypatch,
) -> None:
    previous = _goal()
    expanded = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.99"),
    )

    monkeypatch.setattr(
        economic_goal_module,
        "EconomicGoalContract",
        _GoalSubclass,
    )
    monkeypatch.setattr(
        economic_goal_module,
        "validate_automatic_transition",
        lambda *args, **kwargs: None,
    )
    for helper_name in (
        "_require_same",
        "_require_cap_not_increased",
        "_require_optional_cap_not_increased",
        "_require_floor_not_decreased",
        "_require_int_cap_not_increased",
        "_require_restrictions_not_removed",
    ):
        monkeypatch.setattr(
            economic_goal_module,
            helper_name,
            lambda *args, **kwargs: None,
        )

    with pytest.raises(EconomicGoalContractError, match="max_stake_fraction"):
        validate_automatic_transition(previous, expanded)

    with pytest.raises(EconomicGoalContractError, match="max_stake_fraction"):
        previous.validate_automatic_successor(expanded)


def test_transition_rejects_forged_subclass_after_contract_export_rebinding(
    monkeypatch,
) -> None:
    canonical = _goal()
    forged = _GoalSubclass(
        goal_id=canonical.goal_id,
        revision=2,
        bankroll_id=canonical.bankroll_id,
        currency=canonical.currency,
        objective=canonical.objective,
        max_stake_fraction=Decimal("0"),
        max_stake_amount=canonical.max_stake_amount,
        max_session_loss_fraction=canonical.max_session_loss_fraction,
        max_day_loss_fraction=canonical.max_day_loss_fraction,
        max_drawdown_fraction=canonical.max_drawdown_fraction,
        max_capital_at_risk_fraction=canonical.max_capital_at_risk_fraction,
        max_event_concentration_fraction=canonical.max_event_concentration_fraction,
        max_market_concentration_fraction=canonical.max_market_concentration_fraction,
        max_provider_concentration_fraction=canonical.max_provider_concentration_fraction,
        max_sport_concentration_fraction=canonical.max_sport_concentration_fraction,
        max_turnover_fraction=canonical.max_turnover_fraction,
        max_risk_of_ruin=canonical.max_risk_of_ruin,
        max_execution_slippage_fraction=canonical.max_execution_slippage_fraction,
        max_quote_age_seconds=canonical.max_quote_age_seconds,
        minimum_data_quality=canonical.minimum_data_quality,
        max_concurrent_positions=canonical.max_concurrent_positions,
        max_parlay_legs=canonical.max_parlay_legs,
        automation_level=canonical.automation_level,
        emergency_stop=canonical.emergency_stop,
        blocked_sports=canonical.blocked_sports,
        blocked_providers=canonical.blocked_providers,
        blocked_markets=canonical.blocked_markets,
    )
    monkeypatch.setattr(
        economic_goal_module,
        "EconomicGoalContract",
        _GoalSubclass,
    )

    with pytest.raises(EconomicGoalContractError, match="requires EconomicGoalContract"):
        validate_automatic_transition(canonical, forged)


def test_transition_revalidation_ignores_contract_post_init_rebinding(
    monkeypatch,
) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2)
    object.__setattr__(
        candidate,
        "max_stake_fraction",
        _DecimalSubclass("0.01"),
    )
    monkeypatch.setattr(
        EconomicGoalContract,
        "__post_init__",
        lambda self: None,
    )

    with pytest.raises(EconomicGoalContractError, match="exact Decimal"):
        validate_automatic_transition(previous, candidate)


def test_automatic_transition_revalidates_mutated_candidate_before_comparison() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2)
    _HostileComparable.comparisons = 0
    object.__setattr__(
        candidate,
        "max_stake_fraction",
        _HostileComparable(),
    )

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, candidate)

    assert _HostileComparable.comparisons == 0


def test_automatic_transition_revalidates_mutated_previous_before_comparison() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2)
    _HostileComparable.comparisons = 0
    object.__setattr__(
        previous,
        "max_stake_fraction",
        _HostileComparable(),
    )

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, candidate)

    assert _HostileComparable.comparisons == 0


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
