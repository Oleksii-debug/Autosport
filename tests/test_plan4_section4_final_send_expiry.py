"""Offline §4 falsifiers: actual final-send time, not SUBMITTED, controls receipt TTL.

No bookmaker account, credentials, live transport, or financial movement.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import test_betfair_supervised_execution as fixtures
import test_plan4_section4_confirmation_reuse as confirmation_fixtures
from autosport.betfair_execution_confirmation import (
    CONFIRMATION_FILENAME,
    BetfairExecutionConfirmationError,
    consume_betfair_execution_confirmation,
)
from autosport.betfair_supervised_execution import (
    PlaceOrdersOutcome,
    execute_betfair_supervised_action,
)
from autosport.real_execution_ledger import AttemptState, RealExecutionLedger
from autosport.supervised_confirmation import SupervisedConfirmationAuthority

FINAL_BEFORE_EXPIRY = "2026-09-19T08:00:05+00:00"
FINAL_AFTER_EXPIRY = "2026-09-19T08:00:35+00:00"


def _consume(tmp_path: Path, *, final_send_at: str):
    bound, approval, action, _authority, review, receipt = (
        confirmation_fixtures._issued_confirmation(tmp_path)
    )
    return (
        bound, action, review, receipt,
        lambda: consume_betfair_execution_confirmation(
            tmp_path.resolve(), bound, approval,
            action_id=action.action_id,
            attempt_id="attempt-A",
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            request_sha256=confirmation_fixtures.REQUEST_DIGEST,
            submitted_at=confirmation_fixtures.SUBMITTED_AT,
            final_send_at=final_send_at,
        ),
    )


def test_receipt_expired_after_durable_submission_fails_closed(tmp_path: Path) -> None:
    bound, action, review, receipt, consume = _consume(
        tmp_path, final_send_at=FINAL_AFTER_EXPIRY
    )
    with pytest.raises(BetfairExecutionConfirmationError):
        consume()
    reopened = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: datetime.fromisoformat(FINAL_AFTER_EXPIRY),
    )
    record = reopened.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert record.receipt.consumed_at is None


def test_consumption_is_durable_at_actual_final_send_not_submit(tmp_path: Path) -> None:
    _bound, _action, review, receipt, consume = _consume(
        tmp_path, final_send_at=FINAL_BEFORE_EXPIRY
    )
    witness = consume()
    assert datetime.fromisoformat(witness.consumed_at) == datetime.fromisoformat(
        FINAL_BEFORE_EXPIRY
    )
    reopened = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: datetime.fromisoformat(FINAL_BEFORE_EXPIRY),
    )
    record = reopened.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert record.receipt.consumed_by == witness.consumer_key


def test_final_send_clock_rollback_cannot_consume_receipt(tmp_path: Path) -> None:
    _bound, _action, review, receipt, consume = _consume(
        tmp_path, final_send_at="2026-09-19T08:00:03+00:00"
    )
    with pytest.raises(
        BetfairExecutionConfirmationError, match="precedes the durable submission"
    ):
        consume()
    reopened = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: datetime.fromisoformat(confirmation_fixtures.SUBMITTED_AT),
    )
    record = reopened.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert record.receipt.consumed_at is None


def test_expiry_during_final_send_pretransport_denies_provider_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Valid at durable SUBMITTED, but expired immediately before POST.
    # Quote is still fresh, proving the receipt TTL is the deciding boundary.
    times = iter((fixtures.RESERVED_AT, fixtures.RESERVED_AT, FINAL_AFTER_EXPIRY))
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: next(times, FINAL_AFTER_EXPIRY),
    )
    profile, bound, approval, ledger, action, store = fixtures._prepared(
        str(tmp_path)
    )
    authority, review, receipt = (
        confirmation_fixtures._operator_receipt_for_bound(
            tmp_path, bound, approval, action, attempt_id="ttl-expired"
        )
    )
    transport = fixtures._Transport(
        lambda request: fixtures._response(request)
    )
    client = fixtures._enabled_client(profile, transport, store=store)
    result = execute_betfair_supervised_action(
        ledger, bound, approval,
        action_id=action.action_id,
        attempt_id="ttl-expired",
        profile=profile,
        client=client,
        confirmation_receipt_id=receipt.receipt_id,
        confirmation_review_sha256=review.review_sha256,
    )
    assert result.outcome is PlaceOrdersOutcome.UNKNOWN
    assert transport.calls == []
    assert result.attempt_state is AttemptState.UNKNOWN
    assert RealExecutionLedger(ledger.path).attempt_state(
        "ttl-expired"
    ) is AttemptState.UNKNOWN
    assert not RealExecutionLedger(ledger.path).can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=action.action_id,
    )
    reopened = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: datetime.fromisoformat(FINAL_AFTER_EXPIRY),
    )
    record = reopened.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert record.receipt.consumed_at is None
