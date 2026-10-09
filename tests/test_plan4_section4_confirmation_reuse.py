"""Offline Plan-4 Section-4 confirmation contract falsifiers.

No bookmaker credentials, transport, money movement, or account needed.
The final send adapter must not treat these descriptive witnesses as authority.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

import test_betfair_supervised_execution as existing_fixtures
from autosport.betfair_execution_confirmation import (
    CONFIRMATION_FILENAME,
    BetfairExecutionConfirmationError,
    betfair_execution_confirmation_spec,
    consume_betfair_execution_confirmation,
)
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
)
from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
)

CONFIRMED_AT = datetime.fromisoformat("2026-09-19T08:00:02.200000+00:00")
SUBMITTED_AT = "2026-09-19T08:00:04+00:00"
REQUEST_DIGEST = "d" * 64


def _issued_confirmation(tmp_path: Path, *, attempt_id: str = "attempt-A"):
    profile = existing_fixtures._profile()
    bound, approval, _goal = existing_fixtures._bound(profile)
    action = bound.execution_plan.actions[0]
    spec = betfair_execution_confirmation_spec(
        bound,
        approval,
        action_id=action.action_id,
        attempt_id=attempt_id,
        review_id="review-" + attempt_id,
        risk_evidence_sha256="f" * 64,
    )
    authority = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: CONFIRMED_AT,
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
    return bound, approval, action, authority, review, receipt


def test_confirmation_decision_is_bound_to_exact_attempt_and_risk(tmp_path: Path) -> None:
    bound, approval, action, _authority, review, _receipt = _issued_confirmation(tmp_path)
    unchanged = betfair_execution_confirmation_spec(
        bound, approval,
        action_id=action.action_id,
        attempt_id="attempt-A",
        review_id=review.review_id,
        risk_evidence_sha256="f" * 64,
    )
    other_attempt = betfair_execution_confirmation_spec(
        bound, approval,
        action_id=action.action_id,
        attempt_id="attempt-B",
        review_id="review-attempt-B",
        risk_evidence_sha256="f" * 64,
    )
    other_risk = betfair_execution_confirmation_spec(
        bound, approval,
        action_id=action.action_id,
        attempt_id="attempt-A",
        review_id=review.review_id,
        risk_evidence_sha256="e" * 64,
    )
    assert unchanged.decision_sha256 == review.decision_sha256
    assert other_attempt.decision_sha256 != review.decision_sha256
    assert other_risk.decision_sha256 != review.decision_sha256
    assert other_risk.decision_id == unchanged.decision_id


def test_durable_confirmation_one_shot_survives_restart(tmp_path: Path) -> None:
    bound, approval, action, authority, review, receipt = _issued_confirmation(tmp_path)
    witness = consume_betfair_execution_confirmation(
        tmp_path.resolve(), bound, approval,
        action_id=action.action_id,
        attempt_id="attempt-A",
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        request_sha256=REQUEST_DIGEST,
        submitted_at=SUBMITTED_AT,
    )
    assert witness.request_sha256 == REQUEST_DIGEST
    assert datetime.fromisoformat(witness.consumed_at) == datetime.fromisoformat(SUBMITTED_AT)
    restarted = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: datetime.fromisoformat(SUBMITTED_AT),
    )
    durable = restarted.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert durable.receipt.consumed_by == witness.consumer_key
    with pytest.raises(BetfairExecutionConfirmationError):
        consume_betfair_execution_confirmation(
            tmp_path.resolve(), bound, approval,
            action_id=action.action_id,
            attempt_id="attempt-A",
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            request_sha256=REQUEST_DIGEST,
            submitted_at=SUBMITTED_AT,
        )


def test_foreign_attempt_does_not_consume_receipt(tmp_path: Path) -> None:
    bound, approval, action, authority, review, receipt = _issued_confirmation(tmp_path)
    with pytest.raises(BetfairExecutionConfirmationError):
        consume_betfair_execution_confirmation(
            tmp_path.resolve(), bound, approval,
            action_id=action.action_id,
            attempt_id="attempt-B",
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            request_sha256=REQUEST_DIGEST,
            submitted_at=SUBMITTED_AT,
        )
    assert authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    ).receipt.consumed_at is None


def test_wrong_receipt_digest_and_invalid_request_fail_closed(tmp_path: Path) -> None:
    bound, approval, action, authority, review, receipt = _issued_confirmation(tmp_path)
    for wrong_review, wrong_request in (
        ("0" * 64, REQUEST_DIGEST),
        (review.review_sha256, "not-a-sha"),
    ):
        with pytest.raises(BetfairExecutionConfirmationError):
            consume_betfair_execution_confirmation(
                tmp_path.resolve(), bound, approval,
                action_id=action.action_id,
                attempt_id="attempt-A",
                receipt_id=receipt.receipt_id,
                expected_review_sha256=wrong_review,
                request_sha256=wrong_request,
                submitted_at=SUBMITTED_AT,
            )
    assert authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    ).receipt.consumed_at is None


def test_final_send_admission_failure_never_calls_provider_or_changes_ledger(
    tmp_path: Path,
) -> None:
    """A failed exact-request admission is a pre-I/O denial, not UNKNOWN."""
    profile, bound, _approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(request)
    )
    client = existing_fixtures._enabled_client(
        profile, transport, store=store
    )
    request_digests: list[str] = []

    def reject_final_send(request_sha256: str) -> None:
        request_digests.append(request_sha256)
        raise RuntimeError("sensitive-transport-secret")

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="final-send admission failed before provider transport",
    ) as denied:
        client.place_action(
            action,
            profile=profile,
            bound=bound,
            provider_order_ref="a1b2c3",
            execution_workspace=tmp_path.resolve(),
            _before_transport=reject_final_send,
        )
    assert len(request_digests) == 1
    assert len(request_digests[0]) == 64
    assert all(character in "0123456789abcdef" for character in request_digests[0])
    assert "sensitive-transport-secret" not in str(denied.value)
    assert transport.calls == []
    assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_final_send_admission_sees_exact_request_body_digest(tmp_path: Path) -> None:
    """Admission and report bind identical bytes, not a mutable quote summary."""
    profile, bound, _approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(request)
    )
    client = existing_fixtures._enabled_client(profile, transport, store=store)
    observed: list[str] = []
    report = client.place_action(
        action,
        profile=profile,
        bound=bound,
        provider_order_ref="a1b2c3",
        execution_workspace=tmp_path.resolve(),
        _before_transport=observed.append,
    )
    assert len(transport.calls) == 1
    sent_request = transport.calls[0]["request"]
    encoded = json.dumps(
        sent_request,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    exact_digest = hashlib.sha256(encoded).hexdigest()
    assert observed == [exact_digest]
    assert report.request_sha256 == exact_digest
    assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
