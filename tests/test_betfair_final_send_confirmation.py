from __future__ import annotations

import http.client
import tempfile
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
from autosport.economic_goal import EconomicGoalContract
from autosport.real_execution_ledger import AttemptState
from autosport.supervised_confirmation import SupervisedConfirmationAuthority


_prepared = provider_tests._prepared
_Transport = provider_tests._Transport
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
        clock=lambda: _CONFIRMATION_AT,
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
        ttl_seconds=30,
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
            EconomicGoalContract(
                **{
                    **owner.__dict__,
                    "revision": owner.revision + 1,
                    "emergency_stop": True,
                }
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
