from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.execution_empirical_evidence import (
    EmpiricalExecutionEvidenceError,
    build_empirical_execution_evidence,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


def _issued_reserved_evidence(tmp_path):
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="provider-1",
        account_id="account-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds=Decimal("2.10"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-21T10:00:00+00:00",
        expires_at="2026-09-21T10:01:00+00:00",
    )
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T10:00:01+00:00",
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-21T10:00:01.100000+00:00",
    )
    return build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )


def test_same_object_plan_identity_mutation_revokes_projection_issuance(tmp_path):
    evidence = _issued_reserved_evidence(tmp_path)
    original_hash = evidence.evidence_sha256

    object.__setattr__(evidence, "plan_id", "forged-plan")

    assert evidence._evidence_sha256 == original_hash
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by canonical ledger projection",
    ):
        evidence.assert_projection_issued()


def test_same_object_ledger_identity_mutation_cannot_serialize_as_issued(tmp_path):
    evidence = _issued_reserved_evidence(tmp_path)
    original_hash = evidence.evidence_sha256

    object.__setattr__(evidence, "source_ledger_sha256", "f" * 64)

    assert evidence._evidence_sha256 == original_hash
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by canonical ledger projection",
    ):
        evidence.to_dict()
