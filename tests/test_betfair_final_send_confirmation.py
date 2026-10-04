from __future__ import annotations

import http.client
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

import test_betfair_supervised_execution as provider_tests

from autosport.betfair_execution_confirmation import (
    CONFIRMATION_FILENAME,
    betfair_execution_confirmation_spec,
)
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    PlaceOrdersOutcome,
    execute_betfair_supervised_action,
)
from autosport.real_execution_ledger import AttemptState
from autosport.supervised_confirmation import SupervisedConfirmationAuthority


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
        assert attempt_view.submitted_request_sha256 is not None
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED
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
