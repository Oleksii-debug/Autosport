from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    ExternalAcknowledgement,
    ExternalEffectReconciliation,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


OBSERVED_AT = "2026-09-22T01:00:00+00:00"
RESERVED_AT = "2026-09-22T01:00:01+00:00"
SUBMITTED_AT = "2026-09-22T01:00:02+00:00"
UNKNOWN_AT = "2026-09-22T01:00:03+00:00"
RECONCILED_AT = "2026-09-22T01:00:04+00:00"
ACKNOWLEDGED_AT = "2026-09-22T01:00:05+00:00"
EXPIRES_AT = "2026-09-22T02:00:00+00:00"


def _action(action_id: str, selection_id: str) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234567",
        selection_id=selection_id,
        side="BACK",
        requested_odds="2.20",
        requested_stake="10",
        quote_id=f"quote-{action_id}",
        quote_observed_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-read-view",
        bookmaker_profile_version="betfair-profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=OBSERVED_AT,
        actions=tuple(actions),
    )


def test_verified_execution_view_binds_unknown_facts_to_one_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        ledger = RealExecutionLedger(path)
        first = _action("action-a", "101")
        second = _action("action-b", "202")
        plan = _plan(first, second)
        ledger.reserve_plan(plan)
        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=first.action_id,
            attempt_id="attempt-a",
            reserved_at=RESERVED_AT,
        )
        provider_ref = ledger.bind_provider_order_reference(
            attempt_id="attempt-a",
            provider_id="betfair",
        )
        ledger.mark_submitted("attempt-a", submitted_at=SUBMITTED_AT)
        evidence_id = hashlib.sha256(b"provider-readback-a").hexdigest()
        ledger.bind_provider_evidence(
            attempt_id="attempt-a",
            evidence_id=evidence_id,
            observed_at=SUBMITTED_AT,
            source="betfair:placeOrders:response-a",
        )
        ledger.mark_unknown(
            "attempt-a",
            reason="provider_effect_unresolved",
            observed_at=UNKNOWN_AT,
        )

        view = ledger.verified_execution_view(plan.plan_id)
        exact_snapshot = ledger.verified_snapshot()

        assert view.snapshot_sha256 == exact_snapshot.sha256
        assert view.event_count == exact_snapshot.event_count
        assert view.plan == plan
        assert view.plan_fingerprint == plan.fingerprint
        assert view.stale is False
        assert len(view.plan.actions) == 2
        assert len(view.attempts) == 1

        attempt = view.attempts[0]
        assert attempt.attempt.attempt_id == "attempt-a"
        assert attempt.action == first
        assert attempt.state is AttemptState.UNKNOWN
        assert attempt.submitted_at == SUBMITTED_AT
        assert attempt.unknown_reason == "provider_effect_unresolved"
        assert attempt.unknown_observed_at == UNKNOWN_AT
        assert attempt.provider_order_ref == provider_ref
        assert attempt.provider_evidence is not None
        assert attempt.provider_evidence.evidence_id == evidence_id
        assert attempt.provider_evidence.observed_at == SUBMITTED_AT
        assert attempt.provider_evidence.source == "betfair:placeOrders:response-a"
        assert attempt.acknowledgement is None
        assert attempt.found_reconciliations == ()
        assert attempt.not_found_reconciliation is None

        reopened = RealExecutionLedger(path).verified_execution_view(plan.plan_id)
        assert reopened == view


def test_view_is_immutable_point_in_time_when_ledger_advances() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        ledger = RealExecutionLedger(path)
        first = _action("action-a", "101")
        second = _action("action-b", "202")
        plan = _plan(first, second)
        ledger.reserve_plan(plan)
        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=first.action_id,
            attempt_id="attempt-a",
            reserved_at=RESERVED_AT,
        )
        before = ledger.verified_execution_view(plan.plan_id)

        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=second.action_id,
            attempt_id="attempt-b",
            reserved_at=RESERVED_AT,
        )
        after = ledger.verified_execution_view(plan.plan_id)

        assert len(before.attempts) == 1
        assert len(after.attempts) == 2
        assert before.snapshot_sha256 != after.snapshot_sha256
        assert before.event_count + 1 == after.event_count
        assert before.attempts[0].attempt.attempt_id == "attempt-a"


def test_view_projects_positive_reconciliation_and_terminal_ack() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        ledger = RealExecutionLedger(path)
        action = _action("action-a", "101")
        plan = _plan(action)
        ledger.reserve_plan(plan)
        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=action.action_id,
            attempt_id="attempt-a",
            reserved_at=RESERVED_AT,
        )
        ledger.bind_provider_order_reference(
            attempt_id="attempt-a",
            provider_id="betfair",
        )
        ledger.mark_submitted("attempt-a", submitted_at=SUBMITTED_AT)
        ledger.mark_unknown(
            "attempt-a",
            reason="transport_timeout",
            observed_at=UNKNOWN_AT,
        )
        reconciliation = ExternalEffectReconciliation(
            attempt_id="attempt-a",
            evidence_id="provider-current-orders-1",
            external_receipt_id="bet-123",
            observed_at=RECONCILED_AT,
            source="betfair:listCurrentOrders",
        )
        ledger.reconcile_found(reconciliation)
        acknowledgement = ExternalAcknowledgement(
            attempt_id="attempt-a",
            external_receipt_id="bet-123",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at=ACKNOWLEDGED_AT,
            accepted_odds="2.18",
            accepted_stake="10",
            reconciliation_evidence_id=reconciliation.evidence_id,
        )
        ledger.acknowledge(acknowledgement)

        view = ledger.verified_execution_view(plan.plan_id)
        attempt = view.attempts[0]

        assert attempt.state is AttemptState.ACCEPTED
        assert attempt.acknowledgement == acknowledgement
        assert attempt.found_reconciliations == (reconciliation,)
        assert attempt.not_found_reconciliation is None
        assert view.stale is False

        reopened = RealExecutionLedger(path).verified_execution_view(plan.plan_id)
        assert reopened == view


def test_view_projects_exact_not_found_terminal_fact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        ledger = RealExecutionLedger(path)
        action = _action("action-a", "101")
        plan = _plan(action)
        ledger.reserve_plan(plan)
        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=action.action_id,
            attempt_id="attempt-a",
            reserved_at=RESERVED_AT,
        )
        ledger.mark_submitted("attempt-a", submitted_at=SUBMITTED_AT)
        ledger.mark_unknown(
            "attempt-a",
            reason="transport_timeout",
            observed_at=UNKNOWN_AT,
        )
        not_found = ReconciliationSnapshot(
            attempt_id="attempt-a",
            evidence_id="complete-provider-absence-1",
            observed_at=RECONCILED_AT,
            external_effect_found=False,
            source="provider-readback",
        )
        ledger.reconcile_not_found(not_found)

        view = ledger.verified_execution_view(plan.plan_id)
        attempt = view.attempts[0]

        assert attempt.state is AttemptState.RECONCILED_NOT_FOUND
        assert attempt.not_found_reconciliation == not_found
        assert attempt.found_reconciliations == ()
        assert attempt.acknowledgement is None


def test_verified_execution_view_reestablishes_restart_path_durability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        writer = RealExecutionLedger(path)
        action = _action("action-a", "101")
        plan = _plan(action)
        writer.reserve_plan(plan)

        restarted = RealExecutionLedger(path)

        def fail_directory_sync(self: RealExecutionLedger) -> None:
            raise OSError("injected directory fsync failure")

        monkeypatch.setattr(
            RealExecutionLedger,
            "_sync_parent_directory",
            fail_directory_sync,
        )
        with pytest.raises(
            ExecutionLedgerIntegrityError,
            match="execution ledger durability barrier failed",
        ):
            restarted.verified_execution_view(plan.plan_id)


def test_verified_execution_view_rejects_unknown_plan() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = RealExecutionLedger(Path(tmp) / "real-execution.jsonl")
        with pytest.raises(KeyError):
            ledger.verified_execution_view("missing-plan")
