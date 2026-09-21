from datetime import datetime, timezone

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 18, 20, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


DECISION_SHA = "d" * 64
APPROVAL_SHA = "a" * 64
RISK_A_SHA = "b" * 64
RISK_B_SHA = "c" * 64


def _prepare(
    authority: SupervisedConfirmationAuthority,
    *,
    review_id: str,
    risk_sha: str,
    displayed_risk: str,
):
    return authority.prepare_review(
        review_id=review_id,
        decision_id="decision-1",
        bookmaker_id="betfair",
        account_id="account-1",
        decision_sha256=DECISION_SHA,
        approval_evidence_sha256=APPROVAL_SHA,
        risk_evidence_sha256=risk_sha,
        review_payload={
            "decision": "BACK selection-1",
            "requested_odds": "2.10",
            "requested_stake": "5.00",
            "risk": displayed_risk,
        },
        ttl_seconds=120,
    )


def _require_old_receipt_not_authoritative(
    authority: SupervisedConfirmationAuthority,
    *,
    receipt_id: str,
    review_sha256: str,
) -> None:
    with pytest.raises(SupervisedConfirmationConflictError):
        authority.verify_receipt(
            receipt_id=receipt_id,
            expected_review_sha256=review_sha256,
            require_unconsumed=True,
        )
    with pytest.raises(SupervisedConfirmationConflictError):
        authority.consume_receipt(
            receipt_id=receipt_id,
            expected_review_sha256=review_sha256,
            consumer_key="supervised-plan-issuance:stale-plan",
        )


def test_new_review_after_confirmation_cannot_leave_old_receipt_authoritative(
    tmp_path,
) -> None:
    """New review evidence must not coexist with an older usable receipt."""

    clock = FixedClock()
    path = tmp_path / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)

    review_a = _prepare(
        authority,
        review_id="review-a",
        risk_sha=RISK_A_SHA,
        displayed_risk="risk evidence A",
    )
    receipt_a = authority.confirm_review(
        review_id=review_a.review_id,
        expected_review_sha256=review_a.review_sha256,
    )

    try:
        review_b = _prepare(
            authority,
            review_id="review-b",
            risk_sha=RISK_B_SHA,
            displayed_risk="newer risk evidence B",
        )
    except SupervisedConfirmationConflictError:
        # Safe contract A: once a decision has a live confirmation receipt,
        # a later review for the same immutable decision is rejected.
        return

    assert review_b.decision_id == review_a.decision_id
    assert review_b.decision_sha256 == review_a.decision_sha256
    assert review_b.review_sha256 != review_a.review_sha256

    # Safe contract B: if the newer review is accepted, the older receipt must
    # stop being authority-bearing immediately rather than remaining usable.
    _require_old_receipt_not_authoritative(
        authority,
        receipt_id=receipt_a.receipt_id,
        review_sha256=review_a.review_sha256,
    )

    reopened = SupervisedConfirmationAuthority(path, clock=clock)
    _require_old_receipt_not_authoritative(
        reopened,
        receipt_id=receipt_a.receipt_id,
        review_sha256=review_a.review_sha256,
    )
