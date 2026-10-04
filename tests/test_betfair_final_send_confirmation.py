from __future__ import annotations

import http.client
import tempfile
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest

import test_betfair_supervised_execution as provider_tests

import autosport.betfair_execution_confirmation as confirmation_runtime
import autosport.real_execution_ledger as ledger_runtime
from autosport.betfair_execution_confirmation import (
    CONFIRMATION_FILENAME,
    betfair_execution_confirmation_spec,
)
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    PlaceOrdersOutcome,
    execute_betfair_supervised_action,
)
from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionLedgerBusyError,
    ExecutionStateError,
    RealExecutionLedger,
)
from autosport.supervised_execution import SupervisedExecutionError
from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
)


_prepared = provider_tests._prepared
_Transport = provider_tests._Transport
_TimeoutTransport = provider_tests._TimeoutTransport
_enabled_client = provider_tests._enabled_client
_response = provider_tests._response
SUBMITTED_AT = provider_tests.SUBMITTED_AT

_CONFIRMATION_AT = datetime.fromisoformat("2026-09-19T08:00:02.200000+00:00")
_RISK_EVIDENCE_SHA256 = "f" * 64


@pytest.fixture(autouse=True)
def _canonical_write_network_seam(monkeypatch):
    provider_tests._ACTIVE_WRITE_TRANSPORT = None
    monkeypatch.setattr(
        http.client,
        "HTTPSConnection",
        provider_tests._TestHTTPSConnection,
    )
    try:
        yield
    finally:
        provider_tests._ACTIVE_WRITE_TRANSPORT = None


def _confirmation(
    root: str | Path,
    bound,
    approval,
    action,
    *,
    attempt_id: str,
    review_suffix: str = "exact",
    confirmation_at: datetime = _CONFIRMATION_AT,
    ttl_seconds: int = 30,
):
    workspace = Path(root).resolve()
    spec = betfair_execution_confirmation_spec(
        bound,
        approval,
        action_id=action.action_id,
        attempt_id=attempt_id,
        review_id=f"review-{review_suffix}-{attempt_id}",
        risk_evidence_sha256=_RISK_EVIDENCE_SHA256,
    )
    authority = SupervisedConfirmationAuthority(
        workspace / CONFIRMATION_FILENAME,
        clock=lambda: confirmation_at,
    )
    review = authority.prepare_review(
        review_id=spec.review_id,
        decision_id=spec.decision_id,
        bookmaker_id=spec.bookmaker_id,
        account_id=spec.account_id,
        decision_sha256=spec.decision_sha256,
        approval_evidence_sha256=spec.approval_evidence_sha256,
        risk_evidence_sha256=spec.risk_evidence_sha256,
        review_payload=spec.review_payload,
        ttl_seconds=ttl_seconds,
    )
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    return authority, review, receipt


def _accepted_transport(action):
    return _Transport(
        lambda request: _response(
            request,
            matched=action.requested_stake,
            average=action.requested_odds,
            bet_id="bet-final-confirmation",
            order_status="EXECUTION_COMPLETE",
        )
    )


def test_missing_confirmation_stops_before_submitted_or_provider_io() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="requires durable operator confirmation",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-missing-confirmation",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert (
            ledger.attempt_state("attempt-missing-confirmation")
            is AttemptState.RESERVED
        )


def test_exact_confirmation_is_consumed_before_one_provider_send() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-exact-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
            confirmation_receipt_id=receipt.receipt_id,
            confirmation_review_sha256=review.review_sha256,
        )

        assert result.outcome is PlaceOrdersOutcome.ACCEPTED
        assert result.attempt_state is AttemptState.ACCEPTED
        assert len(transport.calls) == 1
        binding = authority.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            require_unconsumed=False,
        )
        assert binding.receipt.consumed_by is not None
        assert binding.receipt.consumed_by.startswith("betfair-final-send:v1:")
        attempt_view = next(
            item
            for item in ledger.verified_execution_view(
                bound.execution_plan.plan_id
            ).attempts
            if item.attempt.attempt_id == attempt_id
        )
        request_sha256 = sha256(transport.calls[0]["body"]).hexdigest()
        assert attempt_view.submitted_request_sha256 == request_sha256
        assert binding.receipt.consumed_by == confirmation_runtime._consumer_key(
            bound=bound,
            action=action,
            attempt_id=attempt_id,
            request_sha256=request_sha256,
            review_sha256=review.review_sha256,
        )
        assert binding.receipt.consumed_at == attempt_view.submitted_at


def test_receipt_for_other_attempt_fails_after_submitted_and_never_retransmits() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target_attempt = "attempt-target-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id="attempt-other-confirmation",
            review_suffix="other",
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation denied final provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=target_attempt,
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(target_attempt) is AttemptState.SUBMITTED

        restarted = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=target_attempt,
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
            confirmation_receipt_id=receipt.receipt_id,
            confirmation_review_sha256=review.review_sha256,
        )
        assert restarted.outcome is PlaceOrdersOutcome.UNKNOWN
        assert restarted.attempt_state is AttemptState.UNKNOWN
        assert transport.calls == []


def test_already_consumed_receipt_fails_closed_after_durable_submit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-preconsumed-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        authority.consume_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            consumer_key="test-foreign-consumer",
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation denied final provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED


def test_pre_admission_owner_stop_does_not_consume_confirmation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-owner-stop-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)
        owner = goal_store.load()
        goal_store.persist_automatic_successor(
            replace(
                owner,
                revision=owner.revision + 1,
                emergency_stop=True,
            )
        )

        with pytest.raises(BetfairSupervisedExecutionError, match="emergency STOP"):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        binding = authority.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            require_unconsumed=True,
        )
        assert binding.receipt.consumed_at is None


def _audit_receipt(authority, review, receipt):
    return authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )


def test_wrong_review_digest_fails_closed_and_leaves_receipt_unconsumed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-wrong-review-digest"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation denied final provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256="0" * 64,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


def test_confirmation_from_other_workspace_cannot_authorize_final_send() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-foreign-workspace-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        foreign = Path(tmp) / "foreign-confirmation-workspace"
        authority, review, receipt = _confirmation(
            foreign,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation denied final provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


def test_expired_confirmation_fails_closed_after_durable_submit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-expired-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
            confirmation_at=datetime.fromisoformat(
                "2026-09-19T08:00:02+00:00"
            ),
            ttl_seconds=1,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation denied final provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


def test_confirmation_cannot_outlive_underlying_supervised_approval() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-overlong-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
            ttl_seconds=3600,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation denied final provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


@pytest.mark.parametrize(
    "authority_target",
    ("confirmation_consumer", "verified_execution_view"),
)
def test_final_send_fails_closed_if_confirmation_authority_graph_changes(
    monkeypatch,
    authority_target: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = f"attempt-authority-drift-{authority_target}"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        if authority_target == "confirmation_consumer":
            monkeypatch.setattr(
                "autosport.betfair_execution_confirmation."
                "consume_betfair_execution_confirmation",
                lambda *args, **kwargs: None,
            )
        else:
            monkeypatch.setattr(
                provider_tests.betfair_supervised_execution.RealExecutionLedger,
                "verified_execution_view",
                lambda *args, **kwargs: None,
            )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="authority changed|authority",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


@pytest.mark.parametrize(
    "authority_target",
    (
        "coordinated_resolve_root",
        "coordinated_consume_root",
        "generic_module_helper",
        "generic_authority_helper",
        "generic_monotonic_authority_helper",
    ),
)
def test_moving_generic_confirmation_authority_stops_before_provider_transport(
    monkeypatch,
    authority_target: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = f"attempt-generic-authority-drift-{authority_target}"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        with monkeypatch.context() as scoped:
            if authority_target == "coordinated_resolve_root":
                replacement = lambda *args, **kwargs: None
                scoped.setattr(
                    confirmation_runtime._AUTHORITY_TYPE,
                    "resolve_receipt_binding",
                    replacement,
                )
                scoped.setattr(
                    confirmation_runtime,
                    "_RESOLVE_BINDING",
                    replacement,
                )
                scoped.setattr(
                    confirmation_runtime,
                    "_RESOLVE_BINDING_CODE",
                    replacement.__code__,
                )
            elif authority_target == "coordinated_consume_root":
                replacement = lambda *args, **kwargs: None
                scoped.setattr(
                    confirmation_runtime._AUTHORITY_TYPE,
                    "consume_receipt",
                    replacement,
                )
                scoped.setattr(
                    confirmation_runtime,
                    "_CONSUME_RECEIPT",
                    replacement,
                )
                scoped.setattr(
                    confirmation_runtime,
                    "_CONSUME_RECEIPT_CODE",
                    replacement.__code__,
                )
            elif authority_target == "generic_module_helper":
                scoped.setattr(
                    confirmation_runtime._confirmation,
                    "_require_sha256",
                    lambda *args, **kwargs: "0" * 64,
                )
            elif authority_target == "generic_authority_helper":
                scoped.setattr(
                    confirmation_runtime._confirmation.SupervisedConfirmationAuthority,
                    "_load",
                    lambda *args, **kwargs: None,
                )
            else:
                scoped.setattr(
                    confirmation_runtime._confirmation.MonotonicWorkspaceAuthority,
                    "read_history",
                    lambda self: (),
                )

            with pytest.raises(BetfairSupervisedExecutionError, match="authority"):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id=attempt_id,
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                    confirmation_receipt_id=receipt.receipt_id,
                    confirmation_review_sha256=review.review_sha256,
                )

            assert transport.calls == []
            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}

        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


def test_transport_ambiguity_after_confirmation_consumption_never_retransmits() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmed-transport-ambiguity"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _TimeoutTransport()
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
            confirmation_receipt_id=receipt.receipt_id,
            confirmation_review_sha256=review.review_sha256,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert len(transport.calls) == 1
        consumed = _audit_receipt(authority, review, receipt).receipt
        assert consumed.consumed_at is not None
        assert consumed.consumed_by is not None

        restarted = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
            confirmation_receipt_id=receipt.receipt_id,
            confirmation_review_sha256=review.review_sha256,
        )
        assert restarted.outcome is PlaceOrdersOutcome.UNKNOWN
        assert restarted.attempt_state is AttemptState.UNKNOWN
        assert len(transport.calls) == 1
        after_restart = _audit_receipt(authority, review, receipt).receipt
        assert after_restart.consumed_by == consumed.consumed_by
        assert after_restart.consumed_at == consumed.consumed_at


def test_changed_risk_evidence_cannot_rebind_same_final_send_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-risk-evidence-stability"
        profile, bound, approval, ledger, action, _goal_store = _prepared(tmp)
        authority, review, _receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        changed = betfair_execution_confirmation_spec(
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
            review_id="review-risk-evidence-changed",
            risk_evidence_sha256="e" * 64,
        )

        assert changed.decision_id == review.decision_id
        assert changed.decision_sha256 != review.decision_sha256
        with pytest.raises(
            SupervisedConfirmationConflictError,
            match="decision_id is already bound to different durable decision evidence",
        ):
            authority.prepare_review(
                review_id=changed.review_id,
                decision_id=changed.decision_id,
                bookmaker_id=changed.bookmaker_id,
                account_id=changed.account_id,
                decision_sha256=changed.decision_sha256,
                approval_evidence_sha256=changed.approval_evidence_sha256,
                risk_evidence_sha256=changed.risk_evidence_sha256,
                review_payload=changed.review_payload,
                ttl_seconds=30,
            )

        successor_attempt = betfair_execution_confirmation_spec(
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-risk-evidence-successor",
            review_id="review-risk-evidence-successor",
            risk_evidence_sha256="e" * 64,
        )
        assert successor_attempt.decision_id != changed.decision_id


def test_final_send_review_payload_preserves_review_schema_domain() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _profile, bound, approval, _ledger, action, _goal_store = _prepared(tmp)
        spec = betfair_execution_confirmation_spec(
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-review-schema",
            review_id="review-schema",
            risk_evidence_sha256=_RISK_EVIDENCE_SHA256,
        )

        assert spec.review_payload["schema"] == "autosport.betfair_final_send_review"
        assert spec.review_payload["schema_version"] == 1
        assert spec.review_payload["decision_id"] == spec.decision_id
        assert spec.review_payload["decision_sha256"] == spec.decision_sha256



def test_durable_approval_revoked_after_reservation_denies_before_submit_and_confirmation(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-final-durable-approval-revoked"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        competing = RealExecutionLedger(Path(tmp) / "real.jsonl")
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)
        original_bind = ledger.bind_provider_order_reference

        def bind_then_revoke(*args, **kwargs):
            provider_ref = original_bind(*args, **kwargs)
            competing.revoke_supervised_approval(
                plan_id=bound.execution_plan.plan_id,
                approval_id=approval.ledger_identity,
                approval_fingerprint=approval.fingerprint,
                revoked_at=SUBMITTED_AT,
                revocation_evidence_sha256="d" * 64,
            )
            return provider_ref

        monkeypatch.setattr(
            ledger,
            "bind_provider_order_reference",
            bind_then_revoke,
        )

        with pytest.raises(
            SupervisedExecutionError,
            match="durable supervised approval is missing or revoked",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


def test_durable_approval_revocation_is_fenced_through_confirmed_provider_send() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-final-durable-approval-race"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        competing = RealExecutionLedger(Path(tmp) / "real.jsonl")
        revocation_was_fenced = False

        def respond_while_revocation_attempts_to_commit(request):
            nonlocal revocation_was_fenced
            with pytest.raises(ExecutionLedgerBusyError):
                competing.revoke_supervised_approval(
                    plan_id=bound.execution_plan.plan_id,
                    approval_id=approval.ledger_identity,
                    approval_fingerprint=approval.fingerprint,
                    revoked_at=SUBMITTED_AT,
                    revocation_evidence_sha256="e" * 64,
                )
            revocation_was_fenced = True
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
                bet_id="bet-final-durable-approval-race",
                order_status="EXECUTION_COMPLETE",
            )

        transport = _Transport(respond_while_revocation_attempts_to_commit)
        client = _enabled_client(profile, transport, store=goal_store)
        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
            profile=profile,
            client=client,
            confirmation_receipt_id=receipt.receipt_id,
            confirmation_review_sha256=review.review_sha256,
        )

        assert revocation_was_fenced
        assert len(transport.calls) == 1
        assert result.outcome is PlaceOrdersOutcome.ACCEPTED
        assert result.attempt_state is AttemptState.ACCEPTED
        consumed = _audit_receipt(authority, review, receipt).receipt
        assert consumed.consumed_at is not None

        competing.revoke_supervised_approval(
            plan_id=bound.execution_plan.plan_id,
            approval_id=approval.ledger_identity,
            approval_fingerprint=approval.fingerprint,
            revoked_at=SUBMITTED_AT,
            revocation_evidence_sha256="e" * 64,
        )
        assert not ledger.supervised_approval_is_active(
            plan_id=bound.execution_plan.plan_id,
            approval_id=approval.ledger_identity,
            approval_fingerprint=approval.fingerprint,
        )


def test_final_approval_authority_rebinding_fails_before_submit_or_confirmation(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-final-approval-authority-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            provider_tests.betfair_supervised_execution._supervised_execution_runtime,
            "_require_durable_approval",
            lambda *_args, **_kwargs: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="final supervised approval authority changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None



def test_trusted_approval_expiry_at_final_boundary_stays_reserved_and_unconsumed(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-final-trusted-approval-expiry"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)
        trusted_times = iter(
            (provider_tests.RESERVED_AT, provider_tests.APPROVAL_EXPIRES_AT)
        )

        monkeypatch.setattr(
            provider_tests.betfair_supervised_execution._supervised_execution_runtime,
            "_trusted_now",
            lambda: next(trusted_times, provider_tests.APPROVAL_EXPIRES_AT),
        )

        with pytest.raises(
            SupervisedExecutionError,
            match="supervised approval is not active",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: provider_tests.RESERVED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


def test_trusted_quote_expiry_at_final_boundary_stays_reserved_and_unconsumed(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-final-trusted-quote-expiry"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)
        trusted_times = iter(
            (provider_tests.RESERVED_AT, provider_tests.QUOTE_EXPIRES_AT)
        )

        monkeypatch.setattr(
            provider_tests.betfair_supervised_execution._supervised_execution_runtime,
            "_trusted_now",
            lambda: next(trusted_times, provider_tests.QUOTE_EXPIRES_AT),
        )

        with pytest.raises(
            ExecutionStateError,
            match="cannot submit attempt at or after persisted quote expiry",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: provider_tests.RESERVED_AT,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None



def test_final_durable_approval_read_authority_rebinding_fails_closed(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-final-durable-active-authority-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)
        original_bind = ledger.bind_provider_order_reference

        def bind_then_rebind(*args, **kwargs):
            provider_ref = original_bind(*args, **kwargs)
            monkeypatch.setattr(
                RealExecutionLedger,
                "supervised_approval_is_active",
                lambda *_args, **_kwargs: True,
            )
            return provider_ref

        monkeypatch.setattr(
            ledger,
            "bind_provider_order_reference",
            bind_then_rebind,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair internal provider-write authority changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None


def test_final_writer_lock_code_rebinding_fails_closed_before_submission(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-final-writer-lock-authority-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _accepted_transport(action)
        client = _enabled_client(profile, transport, store=goal_store)
        original_bind = ledger.bind_provider_order_reference

        def bind_then_rebind(*args, **kwargs):
            provider_ref = original_bind(*args, **kwargs)
            monkeypatch.setattr(
                ledger_runtime.WorkspaceEconomicLock,
                "acquire",
                lambda self: None,
            )
            return provider_ref

        monkeypatch.setattr(
            ledger,
            "bind_provider_order_reference",
            bind_then_rebind,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair internal provider-write authority changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED
        assert _audit_receipt(authority, review, receipt).receipt.consumed_at is None
