from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionIdentityConflict,
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
        request_sha256 = hashlib.sha256(b"exact-provider-request-a").hexdigest()
        ledger.mark_submitted(
            "attempt-a",
            submitted_at=SUBMITTED_AT,
            request_sha256=request_sha256,
        )
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
        assert attempt.submitted_request_sha256 == request_sha256
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


def test_submitted_request_identity_is_immutable_across_idempotent_replay() -> None:
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
        request_sha256 = hashlib.sha256(b"submitted-request-v1").hexdigest()
        ledger.mark_submitted(
            "attempt-a",
            submitted_at=SUBMITTED_AT,
            request_sha256=request_sha256,
        )
        event_count = ledger.verify_integrity()

        ledger.mark_submitted(
            "attempt-a",
            submitted_at=ACKNOWLEDGED_AT,
            request_sha256=request_sha256,
        )
        assert ledger.verify_integrity() == event_count

        with pytest.raises(
            ExecutionIdentityConflict,
            match="different request identity",
        ):
            ledger.mark_submitted(
                "attempt-a",
                submitted_at=ACKNOWLEDGED_AT,
                request_sha256=hashlib.sha256(b"submitted-request-v2").hexdigest(),
            )

        attempt = RealExecutionLedger(path).verified_execution_view(
            plan.plan_id
        ).attempts[0]
        assert attempt.submitted_at == SUBMITTED_AT
        assert attempt.submitted_request_sha256 == request_sha256


def test_provider_evidence_request_identity_must_match_durable_submission() -> None:
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
        request_sha256 = hashlib.sha256(b"submitted-request-v1").hexdigest()
        ledger.mark_submitted(
            "attempt-a",
            submitted_at=SUBMITTED_AT,
            request_sha256=request_sha256,
        )

        with pytest.raises(
            ExecutionIdentityConflict,
            match="mismatches durable submission",
        ):
            ledger.bind_provider_evidence(
                attempt_id="attempt-a",
                evidence_id=hashlib.sha256(b"provider-report").hexdigest(),
                observed_at=UNKNOWN_AT,
                source="provider:report",
                request_sha256=hashlib.sha256(b"different-request").hexdigest(),
            )

        assert ledger.provider_evidence_binding("attempt-a") is None
        evidence_id = hashlib.sha256(b"provider-report").hexdigest()
        ledger.bind_provider_evidence(
            attempt_id="attempt-a",
            evidence_id=evidence_id,
            observed_at=UNKNOWN_AT,
            source="provider:report",
            request_sha256=request_sha256,
        )

        attempt = RealExecutionLedger(path).verified_execution_view(
            plan.plan_id
        ).attempts[0]
        assert attempt.provider_evidence is not None
        assert attempt.provider_evidence.evidence_id == evidence_id
        assert attempt.provider_evidence.request_sha256 == request_sha256
        assert attempt.provider_evidence.request_sha256 == (
            attempt.submitted_request_sha256
        )


def test_legacy_submission_without_request_digest_remains_readable() -> None:
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

        attempt = RealExecutionLedger(path).verified_execution_view(
            plan.plan_id
        ).attempts[0]
        assert attempt.submitted_at == SUBMITTED_AT
        assert attempt.submitted_request_sha256 is None


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

def test_view_exposes_exact_durable_plan_reservation_epoch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        ledger = RealExecutionLedger(path)
        action = ExecutionAction(
            action_id="action-epoch",
            bookmaker_id="betfair",
            account_id="acct-1",
            event_id="event-epoch",
            market_id="1.epoch",
            selection_id="101",
            side="BACK",
            requested_odds="2.20",
            requested_stake="10",
            quote_id="quote-epoch",
            quote_observed_at="2001-01-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        plan = ExecutionPlan(
            plan_id="plan-epoch",
            bookmaker_profile_version="betfair-profile-v1",
            decision_id="decision-epoch",
            approval_id="approval-epoch",
            created_at="2001-01-01T00:00:01+00:00",
            actions=(action,),
        )

        ledger.reserve_plan(plan)
        snapshot = ledger.verified_snapshot()
        view = ledger.verified_execution_view(plan.plan_id)
        events = RealExecutionLedger._parse(snapshot.payload)
        reserved = [
            event
            for event in events
            if event["plan_id"] == plan.plan_id
            and event["event_type"] == "PLAN_RESERVED"
        ]

        assert len(reserved) == 1
        assert view.snapshot_sha256 == snapshot.sha256
        assert view.event_count == snapshot.event_count
        assert view.plan_reserved_event_id == reserved[0]["event_id"]
        assert view.plan_reserved_at == reserved[0]["recorded_at"]
        assert view.plan_reserved_event_id
        assert view.plan_reserved_at


def test_plan_reservation_epoch_is_independent_from_quote_and_caller_plan_times() -> None:
    from datetime import datetime

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        ledger = RealExecutionLedger(path)
        action = ExecutionAction(
            action_id="action-time",
            bookmaker_id="betfair",
            account_id="acct-1",
            event_id="event-time",
            market_id="1.time",
            selection_id="101",
            side="BACK",
            requested_odds="2.20",
            requested_stake="10",
            quote_id="quote-time",
            quote_observed_at="2001-01-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        plan = ExecutionPlan(
            plan_id="plan-time",
            bookmaker_profile_version="betfair-profile-v1",
            decision_id="decision-time",
            approval_id="approval-time",
            created_at="2001-01-01T00:00:01+00:00",
            actions=(action,),
        )

        ledger.reserve_plan(plan)
        view = ledger.verified_execution_view(plan.plan_id)

        quote_time = datetime.fromisoformat(action.quote_observed_at)
        plan_created_time = datetime.fromisoformat(plan.created_at)
        reserved_time = datetime.fromisoformat(view.plan_reserved_at)
        expiry_time = datetime.fromisoformat(action.expires_at)

        assert quote_time < plan_created_time < reserved_time < expiry_time
        assert view.plan_reserved_at != plan.created_at
        assert view.plan_reserved_at != action.quote_observed_at


def test_exact_plan_redelivery_preserves_original_reservation_epoch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        ledger = RealExecutionLedger(path)
        action = ExecutionAction(
            action_id="action-replay",
            bookmaker_id="betfair",
            account_id="acct-1",
            event_id="event-replay",
            market_id="1.replay",
            selection_id="101",
            side="BACK",
            requested_odds="2.20",
            requested_stake="10",
            quote_id="quote-replay",
            quote_observed_at="2001-01-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        plan = ExecutionPlan(
            plan_id="plan-replay",
            bookmaker_profile_version="betfair-profile-v1",
            decision_id="decision-replay",
            approval_id="approval-replay",
            created_at="2001-01-01T00:00:01+00:00",
            actions=(action,),
        )

        ledger.reserve_plan(plan)
        before = ledger.verified_execution_view(plan.plan_id)
        before_count = ledger.verify_integrity()

        assert ledger.reserve_plan(plan) == plan.fingerprint

        after = ledger.verified_execution_view(plan.plan_id)
        assert ledger.verify_integrity() == before_count
        assert after.plan_reserved_event_id == before.plan_reserved_event_id
        assert after.plan_reserved_at == before.plan_reserved_at
        assert after == before


def test_plan_reservation_epoch_is_restart_stable_and_snapshot_bound() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real-execution.jsonl"
        ledger = RealExecutionLedger(path)
        first = ExecutionAction(
            action_id="action-a",
            bookmaker_id="betfair",
            account_id="acct-1",
            event_id="event-a",
            market_id="1.a",
            selection_id="101",
            side="BACK",
            requested_odds="2.20",
            requested_stake="10",
            quote_id="quote-a",
            quote_observed_at="2001-01-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        second = ExecutionAction(
            action_id="action-b",
            bookmaker_id="betfair",
            account_id="acct-1",
            event_id="event-b",
            market_id="1.b",
            selection_id="202",
            side="BACK",
            requested_odds="2.40",
            requested_stake="5",
            quote_id="quote-b",
            quote_observed_at="2001-01-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        first_plan = ExecutionPlan(
            plan_id="plan-a",
            bookmaker_profile_version="betfair-profile-v1",
            decision_id="decision-a",
            approval_id="approval-a",
            created_at="2001-01-01T00:00:01+00:00",
            actions=(first,),
        )
        second_plan = ExecutionPlan(
            plan_id="plan-b",
            bookmaker_profile_version="betfair-profile-v1",
            decision_id="decision-b",
            approval_id="approval-b",
            created_at="2001-01-01T00:00:01+00:00",
            actions=(second,),
        )

        ledger.reserve_plan(first_plan)
        frozen = ledger.verified_execution_view(first_plan.plan_id)
        restarted = RealExecutionLedger(path).verified_execution_view(first_plan.plan_id)
        assert restarted == frozen

        ledger.reserve_plan(second_plan)
        advanced = ledger.verified_execution_view(first_plan.plan_id)
        other = ledger.verified_execution_view(second_plan.plan_id)

        assert advanced.plan_reserved_event_id == frozen.plan_reserved_event_id
        assert advanced.plan_reserved_at == frozen.plan_reserved_at
        assert advanced.snapshot_sha256 != frozen.snapshot_sha256
        assert advanced.event_count == frozen.event_count + 1
        assert other.plan_reserved_event_id != frozen.plan_reserved_event_id


def test_plan_reservation_epoch_ignores_module_now_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_at = "2002-01-01T00:00:00+00:00"
    calls: list[str] = []

    def hostile_now() -> str:
        calls.append("hostile")
        return hostile_at

    monkeypatch.setattr(ledger_module, "_now", hostile_now)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = RealExecutionLedger(Path(tmp) / "real-execution.jsonl")
        action = ExecutionAction(
            action_id="action-clock-rebind",
            bookmaker_id="betfair",
            account_id="acct-1",
            event_id="event-clock-rebind",
            market_id="1.clock-rebind",
            selection_id="101",
            side="BACK",
            requested_odds="2.20",
            requested_stake="10",
            quote_id="quote-clock-rebind",
            quote_observed_at="2001-01-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        plan = ExecutionPlan(
            plan_id="plan-clock-rebind",
            bookmaker_profile_version="betfair-profile-v1",
            decision_id="decision-clock-rebind",
            approval_id="approval-clock-rebind",
            created_at="2001-01-01T00:00:01+00:00",
            actions=(action,),
        )

        ledger.reserve_plan(plan)
        view = ledger.verified_execution_view(plan.plan_id)

        assert calls == []
        assert view.plan_reserved_at != hostile_at


def test_plan_reservation_epoch_ignores_in_place_now_code_mutation() -> None:
    hostile_at = "2003-01-01T00:00:00+00:00"
    calls: list[str] = []

    def hostile_now() -> str:
        calls.append("hostile")
        return hostile_at

    original_code = ledger_module._now.__code__
    ledger_module._now.__code__ = hostile_now.__code__
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real-execution.jsonl")
            action = ExecutionAction(
                action_id="action-clock-code",
                bookmaker_id="betfair",
                account_id="acct-1",
                event_id="event-clock-code",
                market_id="1.clock-code",
                selection_id="101",
                side="BACK",
                requested_odds="2.20",
                requested_stake="10",
                quote_id="quote-clock-code",
                quote_observed_at="2001-01-01T00:00:00+00:00",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            plan = ExecutionPlan(
                plan_id="plan-clock-code",
                bookmaker_profile_version="betfair-profile-v1",
                decision_id="decision-clock-code",
                approval_id="approval-clock-code",
                created_at="2001-01-01T00:00:01+00:00",
                actions=(action,),
            )

            ledger.reserve_plan(plan)
            view = ledger.verified_execution_view(plan.plan_id)

            assert calls == []
            assert view.plan_reserved_at != hostile_at
    finally:
        ledger_module._now.__code__ = original_code


def test_plan_reservation_event_id_ignores_uuid4_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def hostile_uuid4() -> str:
        calls.append("hostile")
        return "attacker-selected-event-id"

    monkeypatch.setattr(ledger_module.uuid, "uuid4", hostile_uuid4)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = RealExecutionLedger(Path(tmp) / "real-execution.jsonl")
        action = ExecutionAction(
            action_id="action-event-id",
            bookmaker_id="betfair",
            account_id="acct-1",
            event_id="event-event-id",
            market_id="1.event-id",
            selection_id="101",
            side="BACK",
            requested_odds="2.20",
            requested_stake="10",
            quote_id="quote-event-id",
            quote_observed_at="2001-01-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        plan = ExecutionPlan(
            plan_id="plan-event-id",
            bookmaker_profile_version="betfair-profile-v1",
            decision_id="decision-event-id",
            approval_id="approval-event-id",
            created_at="2001-01-01T00:00:01+00:00",
            actions=(action,),
        )

        ledger.reserve_plan(plan)
        view = ledger.verified_execution_view(plan.plan_id)

        assert calls == []
        assert view.plan_reserved_event_id != "attacker-selected-event-id"
        parsed = ledger_module.uuid.UUID(view.plan_reserved_event_id)
        assert parsed.version == 4
