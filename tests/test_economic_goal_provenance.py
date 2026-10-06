from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal

import pytest

from autosport.economic_goal import AutomationLevel, EconomicGoalContract
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


def test_provenance_operations_revalidate_post_construction_mutation() -> None:
    goal = _goal()
    provenance = provenance_for(goal)

    object.__setattr__(goal, "max_stake_fraction", "0.01")
    with pytest.raises(Exception):
        contract_sha256(goal)

    clean_goal = _goal()
    clean_provenance = provenance_for(clean_goal)
    object.__setattr__(clean_provenance, "revision", 0)
    with pytest.raises(EconomicGoalProvenanceError):
        verify_provenance(clean_goal, clean_provenance)
