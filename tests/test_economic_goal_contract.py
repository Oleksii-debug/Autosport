from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
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
        ("max_stake_fraction", Decimal("-0")),
        ("max_turnover_fraction", Decimal("-0E+3")),
        ("minimum_data_quality", Decimal("-0.000")),
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


def test_contract_rejects_scalar_and_container_subclasses() -> None:
    class TextSubclass(str):
        pass

    class DecimalSubclass(Decimal):
        pass

    class IntSubclass(int):
        pass

    class FrozenSetSubclass(frozenset):
        pass

    with pytest.raises(EconomicGoalContractError):
        _goal(goal_id=TextSubclass("goal"))
    with pytest.raises(EconomicGoalContractError):
        _goal(max_stake_fraction=DecimalSubclass("0.01"))
    with pytest.raises(EconomicGoalContractError):
        _goal(revision=IntSubclass(1))
    with pytest.raises(EconomicGoalContractError):
        _goal(blocked_sports=FrozenSetSubclass({"tennis"}))


def test_automatic_transition_revalidates_post_construction_scalar_mutation() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    object.__setattr__(candidate, "max_stake_fraction", "0.01")

    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(previous, candidate)


def test_automatic_transition_rejects_contract_subclasses() -> None:
    class ContractSubclass(EconomicGoalContract):
        pass

    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    subclass = ContractSubclass(
        **{field.name: getattr(candidate, field.name) for field in fields(EconomicGoalContract)}
    )

    with pytest.raises(EconomicGoalContractError, match="requires EconomicGoalContract"):
        validate_automatic_transition(previous, subclass)


def test_contract_successor_ignores_rebound_public_transition_validator(monkeypatch) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))

    def forged_validator(*args, **kwargs):
        raise AssertionError("rebound public validator executed")

    monkeypatch.setattr(
        economic_goal_module,
        "validate_automatic_transition",
        forged_validator,
    )

    previous.validate_automatic_successor(candidate)


def test_contract_successor_ignores_rebound_canonical_transition_alias(monkeypatch) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))

    def forged_validator(*args, **kwargs):
        raise AssertionError("rebound canonical transition alias executed")

    monkeypatch.setattr(
        economic_goal_module,
        "_CANONICAL_TRANSITION_VALIDATOR",
        forged_validator,
    )

    previous.validate_automatic_successor(candidate)


def test_contract_validation_ignores_rebound_public_helpers(monkeypatch) -> None:
    def forged(*args, **kwargs):
        raise AssertionError("rebound validation helper executed")

    for name in (
        "_canonical_text",
        "_positive_int",
        "_fraction",
        "_optional_nonnegative_decimal",
        "_nonnegative_decimal",
        "_nonnegative_int",
        "_canonical_restrictions",
    ):
        monkeypatch.setattr(economic_goal_module, name, forged)

    goal = _goal()
    assert goal.goal_id == "owner-goal-v1"

    candidate = replace(goal, revision=2, max_stake_fraction=Decimal("0.01"))
    validate_automatic_transition(goal, candidate)


def test_automatic_transition_ignores_rebound_authority_guards(monkeypatch) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.03"))

    def forged(*args, **kwargs):
        return None

    for name in (
        "_require_same",
        "_require_cap_not_increased",
        "_require_optional_cap_not_increased",
        "_require_floor_not_decreased",
        "_require_int_cap_not_increased",
        "_require_restrictions_not_removed",
        "_CANONICAL_CONTRACT_VALIDATOR",
    ):
        monkeypatch.setattr(economic_goal_module, name, forged)

    with pytest.raises(EconomicGoalContractError, match="must not increase"):
        validate_automatic_transition(previous, candidate)

    with pytest.raises(EconomicGoalContractError, match="must not increase"):
        previous.validate_automatic_successor(candidate)


def test_automatic_transition_ignores_rebound_contract_type_and_error_bindings(monkeypatch) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.03"))

    monkeypatch.setattr(economic_goal_module, "_CANONICAL_CONTRACT_TYPE", object)
    monkeypatch.setattr(economic_goal_module, "EconomicGoalContractError", RuntimeError)

    with pytest.raises(EconomicGoalContractError, match="must not increase"):
        validate_automatic_transition(previous, candidate)



def test_contract_validation_ignores_rebound_scalar_bounds(monkeypatch) -> None:
    monkeypatch.setattr(economic_goal_module, "_ZERO", Decimal("-999"))
    monkeypatch.setattr(economic_goal_module, "_ONE", Decimal("999"))
    monkeypatch.setattr(economic_goal_module, "_MAX_CANONICAL_TEXT_CHARS", 10000)
    monkeypatch.setattr(economic_goal_module, "_MAX_RESTRICTION_MEMBERS", 10000)

    with pytest.raises(EconomicGoalContractError, match="between 0 and 1"):
        _goal(max_stake_fraction=Decimal("2"))
    with pytest.raises(EconomicGoalContractError, match="non-negative"):
        _goal(max_turnover_fraction=Decimal("-1"))
    with pytest.raises(EconomicGoalContractError, match="text size limit"):
        _goal(goal_id="g" * 513)
    with pytest.raises(EconomicGoalContractError, match="restriction-count limit"):
        _goal(blocked_sports=frozenset(f"sport:{index}" for index in range(1025)))


def test_public_transition_authority_rejects_helper_injection() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))

    with pytest.raises(TypeError):
        validate_automatic_transition(
            previous,
            candidate,
            _cap_guard=lambda *args: None,
        )  # type: ignore[call-arg]


def test_public_transition_ignores_rebound_bound_implementation_alias(monkeypatch) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))

    def forged(*args, **kwargs):
        raise AssertionError("rebound bound transition implementation executed")

    monkeypatch.setattr(
        economic_goal_module,
        "_validate_automatic_transition_bound",
        forged,
    )

    validate_automatic_transition(previous, candidate)
    previous.validate_automatic_successor(candidate)


def test_contract_successor_method_rejects_validator_injection() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.03"))

    with pytest.raises(TypeError):
        previous.validate_automatic_successor(
            candidate,
            _validator=lambda before, after: None,
        )  # type: ignore[call-arg]

    with pytest.raises(EconomicGoalContractError, match="must not increase"):
        previous.validate_automatic_successor(candidate)


def test_transition_proof_uses_isolated_candidate_snapshot() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    canonical_snapshotter = economic_goal_module._snapshot_transition_contract

    def snapshot_then_mutate(contract):
        snapshot = canonical_snapshotter(contract)
        if contract is candidate:
            object.__setattr__(candidate, "max_stake_fraction", Decimal("0.99"))
        return snapshot

    economic_goal_module._validate_automatic_transition_bound(
        previous,
        candidate,
        _snapshotter=snapshot_then_mutate,
    )

    assert candidate.max_stake_fraction == Decimal("0.99")


def test_public_transition_ignores_rebound_snapshotter_alias(monkeypatch) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))

    def forged(*args, **kwargs):
        raise AssertionError("rebound transition snapshotter executed")

    monkeypatch.setattr(economic_goal_module, "_snapshot_transition_contract", forged)

    validate_automatic_transition(previous, candidate)
    previous.validate_automatic_successor(candidate)
