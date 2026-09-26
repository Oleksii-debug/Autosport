from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationError,
)


class _FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _external_root_snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_external_monotonic_root_rejects_protected_workspace_rollback(
    tmp_path,
    monkeypatch,
) -> None:
    machine_root = tmp_path / "machine-authority"
    workspace = tmp_path / "protected-confirmation-workspace"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(machine_root.resolve()),
    )

    clock = _FakeClock()
    path = workspace / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = authority.prepare_review(
        review_id="review-external-root-rollback",
        decision_id="decision-external-root-rollback",
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
    clock.advance(1)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )

    assert machine_root.is_dir()
    assert workspace.resolve() not in machine_root.resolve().parents
    before_consume_root = _external_root_snapshot(machine_root)
    assert before_consume_root

    protected_snapshot = tmp_path / "pre-consumption-protected-snapshot"
    shutil.copytree(workspace, protected_snapshot)

    clock.advance(1)
    authority.consume_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        consumer_key="supervised-plan-issuance:plan-1",
    )
    after_consume_root = _external_root_snapshot(machine_root)
    assert after_consume_root
    assert after_consume_root != before_consume_root

    shutil.rmtree(workspace)
    shutil.copytree(protected_snapshot, workspace)

    # Only the protected application workspace was rolled back.  The independent
    # monotonic authority remains at its post-consumption high-water and must make
    # the stale local state non-authoritative after restart.
    assert _external_root_snapshot(machine_root) == after_consume_root

    try:
        reopened = SupervisedConfirmationAuthority(path, clock=clock)
    except SupervisedConfirmationError:
        return

    with pytest.raises(SupervisedConfirmationError):
        reopened.verify_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )
    with pytest.raises(SupervisedConfirmationError):
        reopened.consume_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            consumer_key="supervised-plan-issuance:plan-2",
        )
