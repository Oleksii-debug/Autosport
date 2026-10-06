from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal

import pytest

import autosport.economic_goal_provenance as economic_goal_provenance_module

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



def test_provenance_derivation_rejects_contract_mutation_between_snapshots() -> None:
    goal = _goal()
    canonical_snapshot = economic_goal_provenance_module._canonical_contract_snapshot
    mutated = False

    def snapshot_then_mutate(contract):
        nonlocal mutated
        snapshot = canonical_snapshot(contract)
        if not mutated:
            object.__setattr__(goal, "goal_id", "mutated-after-snapshot")
            object.__setattr__(goal, "revision", 99)
            mutated = True
        return snapshot

    with pytest.raises(EconomicGoalContractError, match="changed during provenance derivation"):
        economic_goal_provenance_module._provenance_for_bound(
            goal,
            _contract_snapshot=snapshot_then_mutate,
        )

    assert mutated is True


def test_provenance_verification_rejects_contract_mutation_between_snapshots() -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    canonical_snapshot = economic_goal_provenance_module._canonical_contract_snapshot
    mutated = False

    def snapshot_then_mutate(contract):
        nonlocal mutated
        snapshot = canonical_snapshot(contract)
        if not mutated:
            object.__setattr__(goal, "bankroll_id", "mutated-after-snapshot")
            mutated = True
        return snapshot

    with pytest.raises(EconomicGoalContractError, match="changed during provenance verification"):
        economic_goal_provenance_module._verify_provenance_bound(
            goal,
            evidence,
            _contract_snapshot=snapshot_then_mutate,
        )

    assert mutated is True


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

def test_decision_identity_ignores_rebound_snapshotter_alias(monkeypatch) -> None:
    evidence = provenance_for(_goal())
    expected = evidence.decision_identity

    def forged(*args, **kwargs):
        raise AssertionError("rebound provenance snapshotter executed")

    monkeypatch.setattr(economic_goal_provenance_module, "_snapshot_provenance", forged)

    assert evidence.decision_identity == expected


def test_provenance_creation_and_identity_ignore_rebound_constructor(monkeypatch) -> None:
    goal = _goal()
    expected = provenance_for(goal)

    def forged(*args, **kwargs):
        raise AssertionError("rebound EconomicGoalProvenance constructor executed")

    monkeypatch.setattr(EconomicGoalProvenance, "__init__", forged)
    monkeypatch.setattr(EconomicGoalProvenance, "__post_init__", forged)

    evidence = provenance_for(goal)
    assert evidence == expected
    assert evidence.decision_identity == expected.decision_identity
    verify_provenance(goal, evidence)


def test_verification_ignores_rebound_provenance_field_descriptor(monkeypatch) -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    expected_sha = evidence.contract_sha256
    object.__setattr__(evidence, "contract_sha256", "0" * 64)

    class ForgedDescriptor:
        def __get__(self, instance, owner=None):
            return expected_sha

    monkeypatch.setattr(
        EconomicGoalProvenance,
        "contract_sha256",
        ForgedDescriptor(),
    )

    with pytest.raises(EconomicGoalProvenanceError, match="contract_sha256 mismatch"):
        verify_provenance(goal, evidence)


def test_provenance_ignores_rebound_module_evidence_type(monkeypatch) -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    monkeypatch.setattr(
        economic_goal_provenance_module,
        "EconomicGoalProvenance",
        object,
    )

    verify_provenance(goal, evidence)
    assert evidence.decision_identity

def test_provenance_operations_ignore_rebound_snapshot_helpers(monkeypatch) -> None:
    goal = _goal()
    provenance = provenance_for(goal)
    contract_called = False
    provenance_called = False

    def forged_contract(*args: object, **kwargs: object) -> tuple[object, ...]:
        nonlocal contract_called
        contract_called = True
        raise AssertionError("rebound contract snapshot helper executed")

    def forged_provenance(*args: object, **kwargs: object) -> tuple[object, ...]:
        nonlocal provenance_called
        provenance_called = True
        raise AssertionError("rebound provenance snapshot helper executed")

    monkeypatch.setattr(
        economic_goal_provenance_module,
        "_canonical_contract_snapshot",
        forged_contract,
    )
    monkeypatch.setattr(
        economic_goal_provenance_module,
        "_canonical_provenance_snapshot",
        forged_provenance,
    )

    regenerated = provenance_for(goal)
    verify_provenance(goal, provenance)
    assert regenerated.contract_sha256 == provenance.contract_sha256
    assert provenance.decision_identity == (
        f"{provenance.goal_id}@{provenance.revision}:{provenance.contract_sha256}"
    )
    assert contract_called is False
    assert provenance_called is False

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
