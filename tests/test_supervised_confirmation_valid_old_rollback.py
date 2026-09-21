from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationError,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


DECISION_SHA = "d" * 64
APPROVAL_SHA = "a" * 64
RISK_SHA = "b" * 64


def _prepare(authority: SupervisedConfirmationAuthority):
    return authority.prepare_review(
        review_id="review-rollback-1",
        decision_id="decision-rollback-1",
        bookmaker_id="betfair",
        account_id="account-rollback-1",
        decision_sha256=DECISION_SHA,
        approval_evidence_sha256=APPROVAL_SHA,
        risk_evidence_sha256=RISK_SHA,
        review_payload={
            "decision": "BACK selection-rollback-1",
            "requested_odds": "2.10",
            "requested_stake": "5.00",
            "risk": "within external limits",
        },
        ttl_seconds=120,
    )


def _confirmed_authority(path, clock: FakeClock):
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = _prepare(authority)
    clock.advance(seconds=1)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    return authority, review, receipt


def test_valid_old_pair_cannot_resurrect_consumed_receipt(tmp_path) -> None:
    clock = FakeClock()
    path = tmp_path / "operator-confirmations.jsonl"
    authority, review, receipt = _confirmed_authority(path, clock)

    # Snapshot an older state that is internally valid: REVIEW + CONFIRM,
    # before the single-use receipt is consumed.
    old_journal = path.read_bytes()
    old_checkpoint = authority.checkpoint_path.read_bytes()

    clock.advance(seconds=1)
    authority.consume_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        consumer_key="supervised-plan-issuance:plan-rollback-1",
    )

    # First prove the newer durable state really records the one-time use.
    with pytest.raises(SupervisedConfirmationError):
        authority.verify_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )

    # Restore the exact older journal+checkpoint pair. Both files are
    # self-consistent, so a purely local hash chain cannot detect this rollback.
    path.write_bytes(old_journal)
    authority.checkpoint_path.write_bytes(old_checkpoint)

    try:
        rolled_back = SupervisedConfirmationAuthority(path, clock=clock)
    except (SupervisedConfirmationError, RuntimeError, ValueError):
        # A product-owned monotonic witness may reject the stale pair at reopen.
        return

    # If reopen is permitted, neither verification nor a second consumption may
    # regain authority from the stale pair.
    with pytest.raises((SupervisedConfirmationError, RuntimeError, ValueError)):
        rolled_back.verify_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )
    with pytest.raises((SupervisedConfirmationError, RuntimeError, ValueError)):
        rolled_back.consume_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            consumer_key="supervised-plan-issuance:plan-rollback-2",
        )


def test_deleted_local_pair_cannot_rebootstrap_after_committed_confirmation_history(
    tmp_path,
) -> None:
    clock = FakeClock()
    path = tmp_path / "operator-confirmations.jsonl"
    authority, review, receipt = _confirmed_authority(path, clock)

    clock.advance(seconds=1)
    authority.consume_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        consumer_key="supervised-plan-issuance:plan-before-delete",
    )

    # Delete only the local journal/checkpoint pair. A repaired implementation
    # may keep its independent monotonic high-water witness elsewhere in the
    # product workspace; this test intentionally leaves any such witness intact.
    path.unlink()
    authority.checkpoint_path.unlink()

    # Positive confirmation/consumption history must not silently become a
    # pristine authority that can mint a new lineage after restart.
    with pytest.raises((SupervisedConfirmationError, RuntimeError, ValueError)):
        SupervisedConfirmationAuthority(path, clock=clock)
