from __future__ import annotations

from datetime import datetime, timedelta, timezone
import shutil

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
    SupervisedConfirmationError,
)


class _FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _prepare(authority: SupervisedConfirmationAuthority):
    return authority.prepare_review(
        review_id="review-whole-workspace-rollback",
        decision_id="decision-whole-workspace-rollback",
        bookmaker_id="betfair",
        account_id="account-1",
        decision_sha256="d" * 64,
        approval_evidence_sha256="a" * 64,
        risk_evidence_sha256="b" * 64,
        review_payload={
            "decision": "BACK selection-1",
            "requested_odds": "2.10",
            "requested_stake": "5.00",
        },
        ttl_seconds=120,
    )


def test_whole_workspace_rollback_cannot_resurrect_consumed_receipt(tmp_path) -> None:
    clock = _FakeClock()
    workspace = tmp_path / "live-workspace"
    workspace.mkdir()
    path = workspace / "operator-confirmations.jsonl"

    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = _prepare(authority)
    clock.advance(1)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )

    # Snapshot the entire authority workspace, not just journal/checkpoint. The
    # current implementation places MonotonicWorkspaceAuthority under this same
    # directory, so this models a VM/directory restore that rolls back both local
    # state and the supposedly independent high-water witness together.
    snapshot = tmp_path / "pre-consumption-workspace-snapshot"
    shutil.copytree(workspace, snapshot)

    clock.advance(1)
    authority.consume_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        consumer_key="supervised-plan-issuance:plan-1",
    )

    with pytest.raises(SupervisedConfirmationConflictError, match="already been consumed"):
        authority.verify_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )

    shutil.rmtree(workspace)
    shutil.copytree(snapshot, workspace)

    # A safe repair may detect the rollback while reopening. If reopen succeeds,
    # both verification and a second consumption must still fail closed. Restoring
    # every file inside one workspace must never mint a reusable single-use receipt.
    try:
        reopened = SupervisedConfirmationAuthority(path, clock=clock)
    except SupervisedConfirmationError:
        return

    try:
        reopened.verify_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )
    except SupervisedConfirmationError:
        pass
    else:
        pytest.fail("whole-workspace rollback resurrected an unconsumed receipt")

    with pytest.raises(SupervisedConfirmationError):
        reopened.consume_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            consumer_key="supervised-plan-issuance:plan-2",
        )
