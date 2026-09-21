from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 16, 48, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


DECISION_SHA = "d" * 64
APPROVAL_A = "a" * 64
APPROVAL_B = "c" * 64
RISK_A = "b" * 64
RISK_B = "e" * 64


def _prepare(
    authority: SupervisedConfirmationAuthority,
    *,
    review_id: str,
    approval_sha: str,
    risk_sha: str,
    risk_label: str,
):
    return authority.prepare_review(
        review_id=review_id,
        decision_id="decision-1",
        bookmaker_id="betfair",
        account_id="account-1",
        decision_sha256=DECISION_SHA,
        approval_evidence_sha256=approval_sha,
        risk_evidence_sha256=risk_sha,
        review_payload={
            "decision": "BACK selection-1",
            "requested_odds": "2.10",
            "requested_stake": "5.00",
            "risk": risk_label,
        },
        ttl_seconds=120,
    )


def test_newer_pending_review_cannot_leave_older_review_confirmable(tmp_path) -> None:
    """A refreshed operator surface must not leave stale reviewed evidence authoritative."""

    path = tmp_path / "operator-confirmations.jsonl"
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(path, clock=clock)

    older = _prepare(
        authority,
        review_id="review-old",
        approval_sha=APPROVAL_A,
        risk_sha=RISK_A,
        risk_label="risk evidence A",
    )

    try:
        newer = _prepare(
            authority,
            review_id="review-new",
            approval_sha=APPROVAL_B,
            risk_sha=RISK_B,
            risk_label="risk evidence B",
        )
    except SupervisedConfirmationConflictError:
        # A valid repair may enforce one pending review per decision until an
        # explicit supersession/invalidation transition is introduced.
        return

    assert newer.review_sha256 != older.review_sha256

    reopened = SupervisedConfirmationAuthority(path, clock=clock)
    with pytest.raises(
        SupervisedConfirmationConflictError,
        match="supersed|stale|newer|active|pending|review",
    ):
        reopened.confirm_review(
            review_id=older.review_id,
            expected_review_sha256=older.review_sha256,
        )


def test_newer_display_payload_cannot_leave_older_display_confirmable(tmp_path) -> None:
    """Even unchanged upstream digests must not permit two current review surfaces."""

    path = tmp_path / "operator-confirmations.jsonl"
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(path, clock=clock)

    older = _prepare(
        authority,
        review_id="review-old",
        approval_sha=APPROVAL_A,
        risk_sha=RISK_A,
        risk_label="initial displayed risk context",
    )

    try:
        newer = _prepare(
            authority,
            review_id="review-new",
            approval_sha=APPROVAL_A,
            risk_sha=RISK_A,
            risk_label="refreshed displayed risk context",
        )
    except SupervisedConfirmationConflictError:
        return

    assert newer.review_payload_sha256 != older.review_payload_sha256

    with pytest.raises(
        SupervisedConfirmationConflictError,
        match="supersed|stale|newer|active|pending|review",
    ):
        authority.confirm_review(
            review_id=older.review_id,
            expected_review_sha256=older.review_sha256,
        )
