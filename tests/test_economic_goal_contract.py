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


def test_automatic_transition_rejects_post_construction_signed_zero() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    object.__setattr__(candidate, "max_stake_fraction", Decimal("-0"))

    with pytest.raises(EconomicGoalContractError, match="signed zero"):
        validate_automatic_transition(previous, candidate)


def test_automatic_transition_rejects_contract_subclasses() -> None:
    class ContractSubclass(EconomicGoalContract):
        pass

    assert callable(ContractSubclass.validate_automatic_successor)

    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    subclass = ContractSubclass(
        **{field.name: getattr(candidate, field.name) for field in fields(EconomicGoalContract)}
    )

    with pytest.raises(EconomicGoalContractError, match="requires EconomicGoalContract"):
        validate_automatic_transition(previous, subclass)


def test_public_transition_rejects_bound_validator_default_rebinding() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    validator = economic_goal_module._validate_automatic_transition_bound
    original_defaults = validator.__defaults__
    assert original_defaults is not None

    forged_snapshotter = lambda contract: contract
    validator.__defaults__ = (
        forged_snapshotter,
        *original_defaults[1:],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="automatic transition validator defaults authority changed",
        ):
            validate_automatic_transition(previous, candidate)
    finally:
        validator.__defaults__ = original_defaults


def test_public_transition_rejects_nested_snapshotter_default_rebinding() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    snapshotter = economic_goal_module._snapshot_transition_contract
    original_defaults = snapshotter.__defaults__
    assert original_defaults is not None

    snapshotter.__defaults__ = (
        original_defaults[0],
        original_defaults[1],
        object.__new__,
        original_defaults[3],
        original_defaults[4],
        original_defaults[5],
    )
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="automatic transition nested validator defaults authority changed",
        ):
            previous.validate_automatic_successor(candidate)
    finally:
        snapshotter.__defaults__ = original_defaults


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


def test_contract_authority_seal_cannot_be_cleared() -> None:
    assert EconomicGoalContract._authority_operations_sealed is True

    with pytest.raises(
        TypeError,
        match="public authority operation binding is immutable",
    ):
        EconomicGoalContract._authority_operations_sealed = False

    assert EconomicGoalContract._authority_operations_sealed is True


def test_contract_successor_public_method_cannot_be_rebound() -> None:
    original = EconomicGoalContract.validate_automatic_successor

    def forged_validator(*args, **kwargs):
        raise AssertionError("rebound public successor executed")

    with pytest.raises(
        TypeError,
        match="public authority operation binding is immutable",
    ):
        EconomicGoalContract.validate_automatic_successor = forged_validator

    assert EconomicGoalContract.validate_automatic_successor is original

    with pytest.raises(
        TypeError,
        match="public authority operation binding is immutable",
    ):
        del EconomicGoalContract.validate_automatic_successor


def test_contract_authority_rejects_direct_type_mutation() -> None:
    original = EconomicGoalContract.validate_automatic_successor

    def forged_validator(*args, **kwargs):
        raise AssertionError("direct type mutation executed")

    for name, replacement in (
        ("validate_automatic_successor", forged_validator),
        ("_authority_operations_sealed", False),
    ):
        with pytest.raises(
            TypeError,
            match="public authority operation binding is immutable",
        ):
            type.__setattr__(EconomicGoalContract, name, replacement)
        with pytest.raises(
            TypeError,
            match="public authority operation binding is immutable",
        ):
            type.__delattr__(EconomicGoalContract, name)

    assert EconomicGoalContract.validate_automatic_successor is original
    assert EconomicGoalContract._authority_operations_sealed is True


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


def test_transition_authority_rejects_in_place_nested_kwdefault_mutation() -> None:
    def nested_guard(*_args, policy="strict"):
        return None

    def operation(_previous, _candidate, helper=nested_guard):
        helper()

    bound = economic_goal_module._make_transition_validator_authority(operation)
    original_kwdefaults = nested_guard.__kwdefaults__
    assert original_kwdefaults is not None
    original_policy = original_kwdefaults["policy"]

    nested_guard.__kwdefaults__["policy"] = "forged"
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="nested validator keyword defaults authority changed",
        ):
            bound(None, None)
    finally:
        nested_guard.__kwdefaults__["policy"] = original_policy


def test_contract_constructor_rejects_authority_injection() -> None:
    with pytest.raises(TypeError):
        _goal(_validator=lambda value: None)  # type: ignore[arg-type]


def test_contract_constructor_ignores_rebound_module_authorities(monkeypatch) -> None:
    def forged(*args, **kwargs):
        raise AssertionError("rebound constructor authority executed")

    monkeypatch.setattr(economic_goal_module, "_CANONICAL_CONTRACT_VALIDATOR", forged)
    monkeypatch.setattr(economic_goal_module, "_CONTRACT_OBJECT_SETATTR", forged)

    with pytest.raises(EconomicGoalContractError, match="between 0 and 1"):
        _goal(max_stake_fraction=Decimal("2"))


def test_contract_constructor_rejects_bound_default_rebinding() -> None:
    operation = economic_goal_module._contract_init_authority
    original_defaults = operation.__defaults__
    assert original_defaults is not None

    forged_defaults = (
        original_defaults[0],
        Decimal("2"),
        *original_defaults[2:],
    )
    operation.__defaults__ = forged_defaults
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="constructor defaults authority changed",
        ):
            _goal()
    finally:
        operation.__defaults__ = original_defaults


def test_contract_constructor_rejects_code_rebinding() -> None:
    operation = economic_goal_module._contract_init_authority
    original_code = operation.__code__

    def forged(self):
        return None

    operation.__code__ = forged.__code__
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="constructor authority changed",
        ):
            _goal()
    finally:
        operation.__code__ = original_code


def test_contract_constructor_ignores_rebound_post_init(monkeypatch) -> None:
    def forged_post_init(self) -> None:
        raise AssertionError("rebound contract post-init executed")

    monkeypatch.setattr(EconomicGoalContract, "__post_init__", forged_post_init)

    with pytest.raises(EconomicGoalContractError, match="between 0 and 1"):
        _goal(max_stake_fraction=Decimal("2"))


def test_contract_constructor_ignores_rebound_object_writer(monkeypatch) -> None:
    class ForgedObject:
        @staticmethod
        def __setattr__(instance, name, value):
            raise AssertionError("rebound object writer executed")

    monkeypatch.setattr(economic_goal_module, "object", ForgedObject)

    with pytest.raises(EconomicGoalContractError, match="between 0 and 1"):
        _goal(max_stake_fraction=Decimal("2"))


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


def test_automatic_transition_ignores_rebound_automation_ordering_protocol(
    monkeypatch,
) -> None:
    previous = _goal(automation_level=AutomationLevel.SUPERVISED_EXECUTION)
    candidate = replace(
        previous,
        revision=2,
        automation_level=AutomationLevel.BOUNDED_AUTONOMY,
    )

    monkeypatch.setattr(
        AutomationLevel,
        "__gt__",
        lambda self, other: False,
    )
    monkeypatch.setattr(
        AutomationLevel,
        "__index__",
        lambda self: 0,
        raising=False,
    )
    monkeypatch.setattr(
        AutomationLevel,
        "__int__",
        lambda self: 0,
    )

    with pytest.raises(
        EconomicGoalContractError,
        match="must not increase automation_level",
    ):
        validate_automatic_transition(previous, candidate)

    with pytest.raises(
        EconomicGoalContractError,
        match="must not increase automation_level",
    ):
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


def test_automatic_transition_ignores_rebound_contract_field_descriptor(monkeypatch) -> None:
    previous = _goal()
    candidate = replace(
        previous,
        revision=2,
        max_stake_fraction=Decimal("0.03"),
    )

    class ForgedDescriptor:
        def __get__(self, instance, owner=None):
            return Decimal("0.02")

    monkeypatch.setattr(
        EconomicGoalContract,
        "max_stake_fraction",
        ForgedDescriptor(),
    )

    with pytest.raises(EconomicGoalContractError, match="must not increase"):
        validate_automatic_transition(previous, candidate)




def test_transition_snapshot_covers_every_captured_contract_slot(monkeypatch) -> None:
    goal = _goal()
    field_names = economic_goal_module._CONTRACT_FIELD_NAMES
    expected = economic_goal_module._canonical_contract_snapshot(goal)

    for index, name in enumerate(field_names):
        class ForgedDescriptor:
            def __get__(self, instance, owner=None):
                return object()

        monkeypatch.setattr(EconomicGoalContract, name, ForgedDescriptor())
        snapshot = economic_goal_module._canonical_contract_snapshot(goal)
        assert snapshot[index] == expected[index]
        monkeypatch.undo()




def test_transition_snapshot_helper_ignores_rebound_contract_descriptors(monkeypatch) -> None:
    goal = _goal(max_stake_fraction=Decimal("0.03"))

    class ForgedDescriptor:
        def __get__(self, instance, owner=None):
            return Decimal("0.02")

    monkeypatch.setattr(
        EconomicGoalContract,
        "max_stake_fraction",
        ForgedDescriptor(),
    )

    snapshot = economic_goal_module._snapshot_transition_contract(goal)
    assert snapshot.max_stake_fraction == Decimal("0.03")




def test_transition_proof_detects_mutation_between_canonical_snapshots() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    canonical_snapshot = economic_goal_module._canonical_contract_snapshot
    mutated = False

    def snapshot_then_mutate(contract):
        nonlocal mutated
        snapshot = canonical_snapshot(contract)
        if contract is candidate and not mutated:
            object.__setattr__(candidate, "max_stake_fraction", Decimal("0.99"))
            mutated = True
        return snapshot

    with pytest.raises(
        EconomicGoalContractError,
        match="changed during automatic transition validation",
    ):
        economic_goal_module._validate_automatic_transition_bound(
            previous,
            candidate,
            _snapshot=snapshot_then_mutate,
        )

    assert mutated is True



def test_public_transition_ignores_rebound_snapshotter_alias(monkeypatch) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))

    def forged(*args, **kwargs):
        raise AssertionError("rebound transition snapshotter executed")

    monkeypatch.setattr(economic_goal_module, "_snapshot_transition_contract", forged)

    validate_automatic_transition(previous, candidate)
    previous.validate_automatic_successor(candidate)


def test_contract_validator_rejects_transitive_nested_code_mutation() -> None:
    nested_decimal_validator = economic_goal_module._decimal
    original_code = nested_decimal_validator.__code__

    def forged_decimal_validator(_name, value, *args, **kwargs):
        return value

    nested_decimal_validator.__code__ = forged_decimal_validator.__code__
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="economic-goal contract nested validator authority changed",
        ):
            _goal()
    finally:
        nested_decimal_validator.__code__ = original_code


def test_transition_validator_rejects_transitive_contract_validation_mutation() -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    nested_decimal_validator = economic_goal_module._decimal
    original_code = nested_decimal_validator.__code__

    def forged_decimal_validator(_name, value, *args, **kwargs):
        return value

    nested_decimal_validator.__code__ = forged_decimal_validator.__code__
    try:
        with pytest.raises(
            EconomicGoalContractError,
            match="economic-goal contract nested validator authority changed",
        ):
            validate_automatic_transition(previous, candidate)
    finally:
        nested_decimal_validator.__code__ = original_code
