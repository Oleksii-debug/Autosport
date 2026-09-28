from __future__ import annotations

from copy import copy
from dataclasses import fields, replace
from decimal import Decimal
import pickle

import pytest

from autosport.execution_empirical_evidence import (
    EmpiricalExecutionEvidence,
    EmpiricalExecutionEvidenceError,
    build_empirical_execution_evidence,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)

DECISION_AT = "2026-09-21T10:00:01+00:00"
QUOTE_AT = "2026-09-21T10:00:00+00:00"
RESERVED_AT = "2026-09-21T10:00:01.100000+00:00"


def _reserved_evidence(tmp_path) -> EmpiricalExecutionEvidence:
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
        quote_observed_at=QUOTE_AT,
        expires_at="2026-09-21T10:01:00+00:00",
    )
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=DECISION_AT,
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at=RESERVED_AT,
    )
    return build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )


def test_caller_cannot_rebind_verified_ledger_identity_with_dataclass_replace(tmp_path):
    evidence = _reserved_evidence(tmp_path)

    with pytest.raises(EmpiricalExecutionEvidenceError):
        replace(
            evidence,
            source_ledger_sha256="f" * 64,
            source_event_count=evidence.source_event_count + 100,
        )


def test_caller_cannot_rebind_attempt_identity_with_dataclass_replace(tmp_path):
    evidence = _reserved_evidence(tmp_path)

    with pytest.raises(EmpiricalExecutionEvidenceError):
        replace(
            evidence,
            plan_id="forged-plan",
            action_id="forged-action",
            attempt_id="forged-attempt",
            quote_id="forged-quote",
        )


def test_public_constructor_cannot_mint_rebound_projection_without_ledger(tmp_path):
    evidence = _reserved_evidence(tmp_path)
    kwargs = {
        item.name: getattr(evidence, item.name)
        for item in fields(EmpiricalExecutionEvidence)
        if item.init
    }
    kwargs.update(
        source_ledger_sha256="a" * 64,
        plan_id="forged-plan",
        action_id="forged-action",
        attempt_id="forged-attempt",
    )

    with pytest.raises(EmpiricalExecutionEvidenceError):
        EmpiricalExecutionEvidence(**kwargs)


def test_copy_cannot_inherit_projection_issuance(tmp_path):
    evidence = _reserved_evidence(tmp_path)
    duplicated = copy(evidence)

    assert duplicated is not evidence
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by canonical ledger projection",
    ):
        duplicated.assert_projection_issued()
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by canonical ledger projection",
    ):
        duplicated.to_dict()


def test_pickle_round_trip_cannot_inherit_projection_issuance(tmp_path):
    evidence = _reserved_evidence(tmp_path)
    reconstructed = pickle.loads(pickle.dumps(evidence))

    assert reconstructed is not evidence
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by canonical ledger projection",
    ):
        _ = reconstructed.evidence_sha256
