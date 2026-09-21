from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


DECISION_SHA = "d" * 64
OTHER_DECISION_SHA = "e" * 64
APPROVAL_SHA = "a" * 64
RISK_SHA = "b" * 64


def _prepare(
    authority: SupervisedConfirmationAuthority,
    *,
    review_id: str,
    decision_sha256: str = DECISION_SHA,
):
    return authority.prepare_review(
        review_id=review_id,
        decision_id="decision-1",
        bookmaker_id="betfair",
        account_id="account-1",
        decision_sha256=decision_sha256,
        approval_evidence_sha256=APPROVAL_SHA,
        risk_evidence_sha256=RISK_SHA,
        review_payload={
            "decision": "BACK selection-1",
            "requested_odds": "2.10",
            "requested_stake": "5.00",
            "risk": "within external limits",
        },
        ttl_seconds=120,
    )


def _confirm(
    authority: SupervisedConfirmationAuthority,
    review,
):
    return authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )


def test_exact_decision_cannot_mint_two_receipts_through_review_id_alias(tmp_path):
    """Single-use must apply to the execution decision, not only review_id."""

    path = tmp_path / "operator-confirmations.jsonl"
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(path, clock=clock)

    first_review = _prepare(authority, review_id="review-a")
    aliased_review = _prepare(authority, review_id="review-b")

    first_receipt = _confirm(authority, first_review)

    with pytest.raises(SupervisedConfirmationConflictError):
        _confirm(authority, aliased_review)

    reopened = SupervisedConfirmationAuthority(path, clock=clock)
    verified = reopened.verify_receipt(
        receipt_id=first_receipt.receipt_id,
        expected_review_sha256=first_review.review_sha256,
    )
    assert verified.decision_id == "decision-1"

    with pytest.raises(SupervisedConfirmationConflictError):
        _confirm(reopened, aliased_review)


def test_decision_id_cannot_rebind_to_different_decision_digest(tmp_path):
    """The stable decision identity must not be rebound without supersession."""

    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl",
        clock=FakeClock(),
    )
    original = _prepare(authority, review_id="review-a")

    with pytest.raises(SupervisedConfirmationConflictError):
        _prepare(
            authority,
            review_id="review-b",
            decision_sha256=OTHER_DECISION_SHA,
        )

    # The rejected rebinding must not disturb the original review.
    receipt = _confirm(authority, original)
    verified = authority.verify_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=original.review_sha256,
    )
    assert verified.decision_id == "decision-1"
    assert verified.review_sha256 == original.review_sha256
