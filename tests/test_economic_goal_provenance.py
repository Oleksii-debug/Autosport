from __future__ import annotations

from dataclasses import replace
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
        verify_provenance(replace(goal, goal_id="other-goal", revision=2), evidence)

    with pytest.raises(EconomicGoalProvenanceError, match="bankroll_id mismatch"):
        verify_provenance(replace(goal, bankroll_id="other-bankroll", revision=2), evidence)


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
