from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal

import pytest

import autosport.economic_goal as economic_goal_module
import autosport.economic_goal_provenance as economic_goal_provenance_module
import autosport.economic_goal_store as economic_goal_store_module

from autosport.economic_goal import (
    AutomationLevel,
    EconomicGoalContract,
    EconomicGoalContractError,
)
from autosport.economic_goal_provenance import (
    EconomicGoalProvenance,
    EconomicGoalProvenanceError,
    contract_sha256,
    provenance_for,
    verify_provenance,
)
from autosport.economic_goal_store import EconomicGoalStore


def _goal(**changes: object) -> EconomicGoalContract:
    values: dict[str, object] = {
        "goal_id": "owner-goal-v1",
        "revision": 1,
        "bankroll_id": "paper-main",
        "currency": "EUR",
        "max_stake_fraction": Decimal("0.02"),
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
        "blocked_sports": frozenset({"football"}),
        "blocked_providers": frozenset({"provider:a"}),
        "blocked_markets": frozenset({"market:test"}),
    }
    values.update(changes)
    return EconomicGoalContract(**values)  # type: ignore[arg-type]


def test_provenance_is_deterministic_and_revision_specific() -> None:
    goal = _goal()
    first = provenance_for(goal)
    second = provenance_for(goal)

    assert first == second
    assert first.schema_version == 1
    assert first.contract_sha256 == contract_sha256(goal)
    assert first.decision_identity == f"{goal.goal_id}@{goal.revision}:{first.contract_sha256}"

    changed = replace(goal, revision=2, max_stake_fraction=Decimal("0.01"))
    changed_provenance = provenance_for(changed)
    assert changed_provenance.contract_sha256 != first.contract_sha256
    assert changed_provenance.decision_identity != first.decision_identity


def test_decision_identity_rejects_provenance_mutation_between_snapshots() -> None:
    evidence = provenance_for(_goal())
    canonical_snapshot = economic_goal_provenance_module._canonical_provenance_snapshot
    mutated = False

    def snapshot_then_mutate(provenance):
        nonlocal mutated
        snapshot = canonical_snapshot(provenance)
        if not mutated:
            object.__setattr__(evidence, "goal_id", "mutated-after-snapshot")
            object.__setattr__(evidence, "revision", 999)
            mutated = True
        return snapshot

    with pytest.raises(
        EconomicGoalProvenanceError,
        match="changed during identity derivation",
    ):
        economic_goal_provenance_module._decision_identity_bound(
            evidence,
            _snapshot=snapshot_then_mutate,
        )

    assert mutated is True


def test_provenance_authority_seal_cannot_be_cleared() -> None:
    assert EconomicGoalProvenance._authority_operations_sealed is True

    with pytest.raises(
        TypeError,
        match="provenance public authority binding is immutable",
    ):
        EconomicGoalProvenance._authority_operations_sealed = False

    assert EconomicGoalProvenance._authority_operations_sealed is True


def test_decision_identity_public_property_cannot_be_rebound() -> None:
    original = EconomicGoalProvenance.decision_identity

    def forged_property(_instance):
        raise AssertionError("rebound decision identity executed")

    with pytest.raises(
        TypeError,
        match="provenance public authority binding is immutable",
    ):
        EconomicGoalProvenance.decision_identity = forged_property

    assert EconomicGoalProvenance.decision_identity is original

    with pytest.raises(
        TypeError,
        match="provenance public authority binding is immutable",
    ):
        del EconomicGoalProvenance.decision_identity


def test_provenance_authority_rejects_direct_type_mutation() -> None:
    original = EconomicGoalProvenance.decision_identity

    def forged_property(_instance):
        raise AssertionError("direct type mutation executed")

    for name, replacement in (
        ("__init__", forged_property),
        ("__post_init__", forged_property),
        ("decision_identity", forged_property),
        ("_authority_operations_sealed", False),
    ):
        with pytest.raises(
            TypeError,
            match="provenance public authority binding is immutable",
        ):
            type.__setattr__(EconomicGoalProvenance, name, replacement)
        with pytest.raises(
            TypeError,
            match="provenance public authority binding is immutable",
        ):
            type.__delattr__(EconomicGoalProvenance, name)

    assert EconomicGoalProvenance.decision_identity is original
    assert EconomicGoalProvenance._authority_operations_sealed is True


def test_decision_identity_ignores_rebound_bound_implementation(monkeypatch) -> None:
    evidence = provenance_for(_goal())
    expected = evidence.decision_identity
    called = False

    def forged(*args: object, **kwargs: object) -> str:
        nonlocal called
        called = True
        raise AssertionError("rebound decision identity implementation executed")

    monkeypatch.setattr(
        economic_goal_provenance_module,
        "_decision_identity_bound",
        forged,
    )

    assert evidence.decision_identity == expected
    assert called is False


def test_provenance_verifies_after_durable_restart_readback(tmp_path) -> None:
    goal = _goal()
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(goal)

    restored = EconomicGoalStore(tmp_path).load()
    verify_provenance(restored, provenance_for(restored))
    assert restored == goal


def test_provenance_fails_closed_after_contract_tampering() -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    tampered = replace(goal, max_stake_fraction=Decimal("0.019"))

    with pytest.raises(EconomicGoalProvenanceError, match="contract_sha256 mismatch"):
        verify_provenance(tampered, evidence)


def test_provenance_rejects_identity_rebinding() -> None:
    goal = _goal()
    evidence = provenance_for(goal)

    with pytest.raises(EconomicGoalProvenanceError, match="goal_id mismatch"):
        verify_provenance(replace(goal, goal_id="other-goal"), evidence)

    with pytest.raises(EconomicGoalProvenanceError, match="revision mismatch"):
        verify_provenance(replace(goal, revision=2), evidence)

    with pytest.raises(EconomicGoalProvenanceError, match="bankroll_id mismatch"):
        verify_provenance(replace(goal, bankroll_id="other-bankroll"), evidence)


def test_provenance_schema_validation_is_fail_closed() -> None:
    with pytest.raises(EconomicGoalProvenanceError, match="SHA-256 hex"):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="goal",
            revision=1,
            bankroll_id="bankroll",
            contract_sha256="not-a-digest",
        )


def test_provenance_rejects_contract_and_provenance_subclasses() -> None:
    class ContractSubclass(EconomicGoalContract):
        pass

    class ProvenanceSubclass(EconomicGoalProvenance):
        pass

    assert isinstance(ProvenanceSubclass.decision_identity, property)

    goal = _goal()
    contract_subclass = ContractSubclass(
        **{field.name: getattr(goal, field.name) for field in fields(EconomicGoalContract)}
    )
    with pytest.raises((EconomicGoalProvenanceError, TypeError, ValueError)):
        provenance_for(contract_subclass)

    provenance = provenance_for(goal)
    provenance_subclass = ProvenanceSubclass(
        schema=provenance.schema,
        schema_version=provenance.schema_version,
        goal_id=provenance.goal_id,
        revision=provenance.revision,
        bankroll_id=provenance.bankroll_id,
        contract_sha256=provenance.contract_sha256,
    )
    with pytest.raises(EconomicGoalProvenanceError):
        verify_provenance(goal, provenance_subclass)


def test_provenance_rejects_scalar_subclasses() -> None:
    class TextSubclass(str):
        pass

    class IntSubclass(int):
        pass

    with pytest.raises(EconomicGoalProvenanceError):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id=TextSubclass("goal"),
            revision=1,
            bankroll_id="paper-main",
            contract_sha256="0" * 64,
        )
    with pytest.raises(EconomicGoalProvenanceError):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="goal",
            revision=IntSubclass(1),
            bankroll_id="paper-main",
            contract_sha256="0" * 64,
        )


def test_provenance_rejects_schema_subclass_before_comparison() -> None:
    class TextSubclass(str):
        comparisons = 0

        def __eq__(self, other):
            type(self).comparisons += 1
            raise AssertionError("hostile comparison executed")

    with pytest.raises(EconomicGoalProvenanceError, match="unsupported provenance schema"):
        EconomicGoalProvenance(
            schema=TextSubclass("autosport.economic_goal_provenance"),
            schema_version=1,
            goal_id="goal",
            revision=1,
            bankroll_id="paper-main",
            contract_sha256="0" * 64,
        )
    assert TextSubclass.comparisons == 0


def test_provenance_rejects_oversize_identity_text() -> None:
    with pytest.raises(EconomicGoalProvenanceError, match="identity size limit"):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="g" * 513,
            revision=1,
            bankroll_id="paper-main",
            contract_sha256="0" * 64,
        )


def test_decision_identity_revalidates_post_construction_mutation() -> None:
    provenance = provenance_for(_goal())
    object.__setattr__(provenance, "goal_id", "")

    with pytest.raises(EconomicGoalProvenanceError):
        _ = provenance.decision_identity


def test_decision_identity_ignores_rebound_provenance_validator(monkeypatch) -> None:
    provenance = provenance_for(_goal())
    object.__setattr__(provenance, "goal_id", "")

    monkeypatch.setattr(EconomicGoalProvenance, "__post_init__", lambda self: None)

    with pytest.raises(EconomicGoalProvenanceError):
        _ = provenance.decision_identity


def test_provenance_operations_ignore_rebound_contract_validator(monkeypatch) -> None:
    goal = _goal()
    object.__setattr__(goal, "max_stake_fraction", "0.01")

    monkeypatch.setattr(EconomicGoalContract, "__post_init__", lambda self: None)
    monkeypatch.setattr(
        economic_goal_provenance_module,
        "EconomicGoalContract",
        object,
    )

    with pytest.raises(EconomicGoalContractError):
        contract_sha256(goal)


def test_provenance_operations_revalidate_post_construction_mutation() -> None:
    goal = _goal()
    provenance = provenance_for(goal)

    object.__setattr__(goal, "max_stake_fraction", "0.01")
    with pytest.raises(EconomicGoalContractError):
        contract_sha256(goal)

    clean_goal = _goal()
    clean_provenance = provenance_for(clean_goal)
    object.__setattr__(clean_provenance, "revision", 0)
    with pytest.raises(EconomicGoalProvenanceError):
        verify_provenance(clean_goal, clean_provenance)


def test_contract_sha256_rejects_bound_default_rebinding() -> None:
    contract = _goal()
    operation = economic_goal_provenance_module._contract_sha256_bound
    original_defaults = operation.__defaults__
    assert original_defaults is not None

    operation.__defaults__ = (
        object,
        *original_defaults[1:],
    )
    try:
        with pytest.raises(
            EconomicGoalProvenanceError,
            match="provenance operation defaults authority changed",
        ):
            contract_sha256(contract)
    finally:
        operation.__defaults__ = original_defaults


def test_decision_identity_rejects_snapshotter_default_rebinding() -> None:
    evidence = provenance_for(_goal())
    snapshotter = economic_goal_provenance_module._snapshot_provenance
    original_defaults = snapshotter.__defaults__
    assert original_defaults is not None

    snapshotter.__defaults__ = (
        original_defaults[0],
        object,
        original_defaults[2],
    )
    try:
        with pytest.raises(
            EconomicGoalProvenanceError,
            match="provenance decision identity nested defaults authority changed",
        ):
            _ = evidence.decision_identity
    finally:
        snapshotter.__defaults__ = original_defaults


def test_provenance_verifier_rejects_nested_hash_default_rebinding() -> None:
    contract = _goal()
    evidence = provenance_for(contract)
    operation = economic_goal_provenance_module._contract_sha256_bound
    original_defaults = operation.__defaults__
    assert original_defaults is not None

    operation.__defaults__ = (
        original_defaults[0],
        original_defaults[1],
        original_defaults[2],
        original_defaults[3],
        lambda payload: b"forged",
        original_defaults[5],
        original_defaults[6],
    )
    try:
        with pytest.raises(
            EconomicGoalProvenanceError,
            match="provenance verification nested defaults authority changed",
        ):
            verify_provenance(contract, evidence)
    finally:
        operation.__defaults__ = original_defaults


def test_contract_sha256_ignores_rebound_hashing_dispatch(monkeypatch) -> None:
    goal = _goal()
    expected = contract_sha256(goal)

    def forged(*args, **kwargs):
        raise AssertionError("rebound provenance hashing dependency executed")

    monkeypatch.setattr(economic_goal_provenance_module, "_canonical_json", forged)
    monkeypatch.setattr(economic_goal_provenance_module, "economic_goal_to_payload", forged)
    monkeypatch.setattr(economic_goal_provenance_module, "_CANONICAL_GOAL_VALIDATOR", forged)
    monkeypatch.setattr(economic_goal_provenance_module.hashlib, "sha256", forged)

    assert contract_sha256(goal) == expected


def test_provenance_operations_ignore_rebound_internal_authorities(monkeypatch) -> None:
    goal = _goal()
    expected = provenance_for(goal)

    def forged(*args, **kwargs):
        raise AssertionError("rebound provenance authority executed")

    monkeypatch.setattr(economic_goal_provenance_module, "contract_sha256", forged)
    monkeypatch.setattr(economic_goal_provenance_module, "_CANONICAL_GOAL_VALIDATOR", forged)
    monkeypatch.setattr(economic_goal_provenance_module, "_CANONICAL_PROVENANCE_VALIDATOR", forged)
    monkeypatch.setattr(economic_goal_provenance_module, "_CANONICAL_PROVENANCE_TYPE", object)
    monkeypatch.setattr(economic_goal_provenance_module, "PROVENANCE_SCHEMA", "forged")
    monkeypatch.setattr(economic_goal_provenance_module, "PROVENANCE_SCHEMA_VERSION", 999)

    evidence = provenance_for(goal)
    assert evidence == expected
    verify_provenance(goal, evidence)


def test_provenance_validation_ignores_rebound_schema_bounds_and_error(monkeypatch) -> None:
    monkeypatch.setattr(economic_goal_provenance_module, "PROVENANCE_SCHEMA", "forged")
    monkeypatch.setattr(economic_goal_provenance_module, "PROVENANCE_SCHEMA_VERSION", 999)
    monkeypatch.setattr(economic_goal_provenance_module, "_MAX_PROVENANCE_IDENTITY_CHARS", 10000)
    monkeypatch.setattr(economic_goal_provenance_module, "EconomicGoalProvenanceError", RuntimeError)

    with pytest.raises(EconomicGoalProvenanceError, match="identity size limit"):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="g" * 513,
            revision=1,
            bankroll_id="paper-main",
            contract_sha256="0" * 64,
        )


def test_public_provenance_authority_operations_reject_helper_injection() -> None:
    goal = _goal()
    evidence = provenance_for(goal)

    with pytest.raises(TypeError):
        contract_sha256(goal, _goal_validator=lambda contract: None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        provenance_for(goal, _contract_sha256=lambda contract: "0" * 64)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        verify_provenance(
            goal,
            evidence,
            _provenance_validator=lambda provenance: None,
        )  # type: ignore[call-arg]


def test_public_provenance_operations_ignore_rebound_bound_implementation_aliases(
    monkeypatch,
) -> None:
    goal = _goal()
    expected = provenance_for(goal)

    def forged(*args, **kwargs):
        raise AssertionError("rebound bound implementation alias executed")

    monkeypatch.setattr(economic_goal_provenance_module, "_contract_sha256_bound", forged)
    monkeypatch.setattr(economic_goal_provenance_module, "_provenance_for_bound", forged)
    monkeypatch.setattr(economic_goal_provenance_module, "_verify_provenance_bound", forged)

    assert contract_sha256(goal) == expected.contract_sha256
    evidence = provenance_for(goal)
    assert evidence == expected
    verify_provenance(goal, evidence)


def test_provenance_identity_is_bound_to_one_canonical_contract_snapshot() -> None:
    goal = _goal()
    canonical_encoder = economic_goal_provenance_module.economic_goal_to_payload

    def encode_then_mutate(contract):
        payload = canonical_encoder(contract)
        object.__setattr__(goal, "goal_id", "mutated-after-snapshot")
        object.__setattr__(goal, "revision", 99)
        return payload

    evidence = economic_goal_provenance_module._provenance_for_bound(
        goal,
        _payload_encoder=encode_then_mutate,
    )

    assert evidence.goal_id == "owner-goal-v1"
    assert evidence.revision == 1
    assert goal.goal_id == "mutated-after-snapshot"
    assert goal.revision == 99


def test_provenance_verification_uses_canonical_contract_snapshot_after_capture() -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    canonical_encoder = economic_goal_provenance_module.economic_goal_to_payload

    def encode_then_mutate(contract):
        payload = canonical_encoder(contract)
        object.__setattr__(goal, "bankroll_id", "mutated-after-snapshot")
        return payload

    economic_goal_provenance_module._verify_provenance_bound(
        goal,
        evidence,
        _payload_encoder=encode_then_mutate,
    )

    assert goal.bankroll_id == "mutated-after-snapshot"


def test_decision_identity_uses_isolated_provenance_snapshot() -> None:
    evidence = provenance_for(_goal())
    expected = evidence.decision_identity
    canonical_snapshotter = economic_goal_provenance_module._snapshot_provenance

    def snapshot_then_mutate(provenance):
        snapshot = canonical_snapshotter(provenance)
        object.__setattr__(evidence, "goal_id", "mutated-after-snapshot")
        object.__setattr__(evidence, "revision", 999)
        return snapshot

    actual = economic_goal_provenance_module._decision_identity_bound(
        evidence,
        _snapshotter=snapshot_then_mutate,
    )

    assert actual == expected
    assert evidence.goal_id == "mutated-after-snapshot"
    assert evidence.revision == 999


def test_decision_identity_ignores_rebound_snapshotter_alias(monkeypatch) -> None:
    evidence = provenance_for(_goal())
    expected = evidence.decision_identity

    def forged(*args, **kwargs):
        raise AssertionError("rebound provenance snapshotter executed")

    monkeypatch.setattr(economic_goal_provenance_module, "_snapshot_provenance", forged)

    assert evidence.decision_identity == expected


def test_provenance_constructor_rejects_authority_injection() -> None:
    with pytest.raises(TypeError):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="owner-goal-v1",
            revision=1,
            bankroll_id="paper-main",
            contract_sha256="0" * 64,
            _validator=lambda value: None,  # type: ignore[call-arg]
        )


def test_provenance_constructor_ignores_rebound_module_authorities(monkeypatch) -> None:
    def forged(*args, **kwargs):
        raise AssertionError("rebound provenance constructor authority executed")

    monkeypatch.setattr(economic_goal_provenance_module, "_CANONICAL_PROVENANCE_VALIDATOR", forged)
    monkeypatch.setattr(economic_goal_provenance_module, "_PROVENANCE_OBJECT_SETATTR", forged)

    with pytest.raises(EconomicGoalProvenanceError, match="SHA-256 hex"):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="owner-goal-v1",
            revision=1,
            bankroll_id="paper-main",
            contract_sha256="not-a-sha",
        )


def test_provenance_constructor_rejects_code_rebinding(monkeypatch) -> None:
    operation = economic_goal_provenance_module._provenance_init_authority
    original_code = operation.__code__

    def forged(self):
        return None

    operation.__code__ = forged.__code__
    try:
        with pytest.raises(
            EconomicGoalProvenanceError,
            match="constructor authority changed",
        ):
            EconomicGoalProvenance(
                schema="autosport.economic_goal_provenance",
                schema_version=1,
                goal_id="owner-goal-v1",
                revision=1,
                bankroll_id="paper-main",
                contract_sha256="not-a-sha",
            )
    finally:
        operation.__code__ = original_code


def test_provenance_constructor_and_post_init_bindings_are_sealed() -> None:
    def forged(*args, **kwargs):
        raise AssertionError("rebound provenance constructor authority executed")

    for name in ("__init__", "__post_init__"):
        with pytest.raises(
            TypeError,
            match="provenance public authority binding is immutable",
        ):
            setattr(EconomicGoalProvenance, name, forged)
        with pytest.raises(
            TypeError,
            match="provenance public authority binding is immutable",
        ):
            type.__setattr__(EconomicGoalProvenance, name, forged)

    with pytest.raises(EconomicGoalProvenanceError, match="SHA-256 hex"):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="owner-goal-v1",
            revision=1,
            bankroll_id="paper-main",
            contract_sha256="not-a-sha",
        )


def test_provenance_constructor_ignores_rebound_object_writer(monkeypatch) -> None:
    class ForgedObject:
        @staticmethod
        def __setattr__(instance, name, value):
            raise AssertionError("rebound object writer executed")

    monkeypatch.setattr(economic_goal_provenance_module, "object", ForgedObject)

    with pytest.raises(EconomicGoalProvenanceError, match="SHA-256 hex"):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="owner-goal-v1",
            revision=1,
            bankroll_id="paper-main",
            contract_sha256="not-a-sha",
        )


def test_contract_hash_ignores_descriptor_laundering(monkeypatch) -> None:
    goal = _goal(max_stake_fraction=Decimal("0.03"))
    expected = economic_goal_provenance_module.contract_sha256(goal)

    class ForgedDescriptor:
        def __get__(self, instance, owner=None):
            return Decimal("0.02")

    monkeypatch.setattr(
        EconomicGoalContract,
        "max_stake_fraction",
        ForgedDescriptor(),
    )

    assert economic_goal_provenance_module.contract_sha256(goal) == expected




def test_provenance_snapshot_covers_every_captured_field(monkeypatch) -> None:
    evidence = provenance_for(_goal())
    field_names = economic_goal_provenance_module._PROVENANCE_FIELD_NAMES
    expected = economic_goal_provenance_module._canonical_provenance_snapshot(
        evidence
    )

    for index, name in enumerate(field_names):
        class ForgedDescriptor:
            def __get__(self, instance, owner=None):
                return object()

        monkeypatch.setattr(EconomicGoalProvenance, name, ForgedDescriptor())
        snapshot = economic_goal_provenance_module._canonical_provenance_snapshot(
            evidence
        )
        assert snapshot[index] == expected[index]
        monkeypatch.undo()




def test_snapshot_provenance_ignores_rebound_field_descriptors(monkeypatch) -> None:
    evidence = provenance_for(_goal())
    expected_sha = evidence.contract_sha256

    class ForgedDescriptor:
        def __get__(self, instance, owner=None):
            return "0" * 64

    monkeypatch.setattr(
        EconomicGoalProvenance,
        "contract_sha256",
        ForgedDescriptor(),
    )

    snapshot = economic_goal_provenance_module._snapshot_provenance(evidence)
    assert snapshot.contract_sha256 == expected_sha




def test_decision_identity_ignores_rebound_provenance_field_descriptors(monkeypatch) -> None:
    evidence = provenance_for(_goal())
    expected = evidence.decision_identity

    class ForgedDescriptor:
        def __get__(self, instance, owner=None):
            return "0" * 64

    monkeypatch.setattr(
        EconomicGoalProvenance,
        "contract_sha256",
        ForgedDescriptor(),
    )

    assert evidence.decision_identity == expected


def test_provenance_creation_and_identity_use_sealed_constructor_authority() -> None:
    goal = _goal()
    expected = provenance_for(goal)

    def forged(*args, **kwargs):
        raise AssertionError("rebound EconomicGoalProvenance constructor executed")

    for name in ("__init__", "__post_init__"):
        with pytest.raises(
            TypeError,
            match="provenance public authority binding is immutable",
        ):
            setattr(EconomicGoalProvenance, name, forged)

    evidence = provenance_for(goal)
    assert evidence == expected
    assert evidence.decision_identity == expected.decision_identity
    verify_provenance(goal, evidence)

def test_contract_sha256_rejects_bound_keyword_default_rebinding() -> None:
    contract = _goal()
    operation = economic_goal_provenance_module._contract_sha256_bound
    original_kwdefaults = operation.__kwdefaults__

    operation.__kwdefaults__ = {"forged_authority": object()}
    try:
        with pytest.raises(
            EconomicGoalProvenanceError,
            match="provenance operation keyword defaults authority changed",
        ):
            contract_sha256(contract)
    finally:
        operation.__kwdefaults__ = original_kwdefaults


def test_provenance_for_rejects_in_place_builder_keyword_default_mutation() -> None:
    contract = _goal()
    builder = economic_goal_provenance_module._build_provenance
    original_kwdefaults = builder.__kwdefaults__
    assert original_kwdefaults is not None
    original_validator = original_kwdefaults["_validator"]

    original_kwdefaults["_validator"] = lambda _evidence: None
    try:
        with pytest.raises(
            EconomicGoalProvenanceError,
            match="provenance operation nested keyword defaults authority changed",
        ):
            provenance_for(contract)
    finally:
        original_kwdefaults["_validator"] = original_validator


def test_provenance_contract_field_order_ignores_runtime_rebinding(monkeypatch) -> None:
    contract = _goal()
    canonical = provenance_for(contract)

    monkeypatch.setattr(
        economic_goal_provenance_module,
        "_PROVENANCE_CONTRACT_FIELD_NAMES",
        tuple(reversed(economic_goal_provenance_module._PROVENANCE_CONTRACT_FIELD_NAMES)),
    )

    rebound = provenance_for(contract)
    assert rebound == canonical


def test_provenance_operation_rejects_transitive_json_encoder_code_mutation() -> None:
    goal = _goal()
    nested_json_encoder = economic_goal_provenance_module._canonical_json
    original_code = nested_json_encoder.__code__

    def forged_json_encoder(_payload, *args, **kwargs):
        return b"forged"

    nested_json_encoder.__code__ = forged_json_encoder.__code__
    try:
        with pytest.raises(
            EconomicGoalProvenanceError,
            match="economic-goal provenance operation nested authority changed",
        ):
            provenance_for(goal)
    finally:
        nested_json_encoder.__code__ = original_code


def test_provenance_verifier_rejects_transitive_hash_dependency_mutation() -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    nested_json_encoder = economic_goal_provenance_module._canonical_json
    original_code = nested_json_encoder.__code__

    def forged_json_encoder(_payload, *args, **kwargs):
        return b"forged"

    nested_json_encoder.__code__ = forged_json_encoder.__code__
    try:
        with pytest.raises(
            EconomicGoalProvenanceError,
            match="economic-goal provenance verification nested authority changed",
        ):
            verify_provenance(goal, evidence)
    finally:
        nested_json_encoder.__code__ = original_code


def test_provenance_verifier_maps_contract_identity_fields_by_canonical_position() -> None:
    goal = _goal(
        goal_id="goal-distinct",
        revision=7,
        bankroll_id="bankroll-distinct",
        currency="EUR",
    )
    evidence = provenance_for(goal)

    # A valid canonical provenance must verify before any mismatch falsifier.
    verify_provenance(goal, evidence)

    for field, value, message in (
        ("goal_id", "other-goal", "goal_id mismatch"),
        ("revision", 8, "revision mismatch"),
        ("bankroll_id", "other-bankroll", "bankroll_id mismatch"),
    ):
        with pytest.raises(EconomicGoalProvenanceError, match=message):
            verify_provenance(replace(goal, **{field: value}), evidence)


def test_economic_goal_field_order_is_shared_across_contract_store_and_provenance() -> None:
    assert economic_goal_provenance_module._PROVENANCE_CONTRACT_FIELD_NAMES == (
        economic_goal_module._CONTRACT_FIELD_NAMES
    )
    assert economic_goal_store_module._CONTRACT_KEYS_ORDERED == (
        economic_goal_module._CONTRACT_FIELD_NAMES
    )
