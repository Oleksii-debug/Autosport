from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
    SupervisedConfirmationIntegrityError,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


FAIL_CLOSED_ERRORS = (
    SupervisedConfirmationConflictError,
    SupervisedConfirmationIntegrityError,
)
DECISION_SHA = "d" * 64
APPROVAL_SHA = "a" * 64
RISK_SHA = "b" * 64


def _prepare(
    authority: SupervisedConfirmationAuthority,
    *,
    ttl_seconds: int = 10,
):
    return authority.prepare_review(
        review_id="review-expiry-1",
        decision_id="decision-expiry-1",
        bookmaker_id="betfair",
        account_id="account-1",
        decision_sha256=DECISION_SHA,
        approval_evidence_sha256=APPROVAL_SHA,
        risk_evidence_sha256=RISK_SHA,
        review_payload={
            "decision": "BACK selection-1",
            "requested_odds": "2.10",
            "requested_stake": "5.00",
            "risk": "within external limits",
        },
        ttl_seconds=ttl_seconds,
    )


def test_confirmation_fails_closed_at_exact_review_expiry_boundary(tmp_path):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl",
        clock=clock,
    )
    review = _prepare(authority, ttl_seconds=10)

    clock.advance(seconds=10)

    with pytest.raises(FAIL_CLOSED_ERRORS):
        authority.confirm_review(
            review_id=review.review_id,
            expected_review_sha256=review.review_sha256,
        )

    assert authority.path.read_text(encoding="utf-8").count("\n") == 1


def test_unconsumed_verify_cannot_authorize_after_review_expiry(tmp_path):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl",
        clock=clock,
    )
    review = _prepare(authority, ttl_seconds=10)

    clock.advance(seconds=1)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    clock.advance(seconds=10)

    with pytest.raises(FAIL_CLOSED_ERRORS):
        authority.verify_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            require_unconsumed=True,
        )

    assert authority.path.read_text(encoding="utf-8").count("\n") == 2


def test_consume_cannot_use_receipt_after_bound_review_expiry(tmp_path):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl",
        clock=clock,
    )
    review = _prepare(authority, ttl_seconds=10)

    clock.advance(seconds=1)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    clock.advance(seconds=10)

    with pytest.raises(FAIL_CLOSED_ERRORS):
        authority.consume_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            consumer_key="supervised-plan-issuance:plan-expiry-1",
        )

    assert authority.path.read_text(encoding="utf-8").count("\n") == 2


def test_clock_rewind_cannot_confirm_before_review_time(tmp_path):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl",
        clock=clock,
    )
    review = _prepare(authority, ttl_seconds=10)

    clock.advance(seconds=-1)

    with pytest.raises(FAIL_CLOSED_ERRORS):
        authority.confirm_review(
            review_id=review.review_id,
            expected_review_sha256=review.review_sha256,
        )

    assert authority.path.read_text(encoding="utf-8").count("\n") == 1
