from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationIntegrityError,
)


DECISION_SHA = "d" * 64
APPROVAL_SHA = "a" * 64
RISK_SHA = "b" * 64


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def _prepare_confirmed(
    path: Path,
    *,
    clock: FixedClock,
) -> SupervisedConfirmationAuthority:
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = authority.prepare_review(
        review_id="review-1",
        decision_id="decision-1",
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
        ttl_seconds=120,
    )
    authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    return authority


def test_nonempty_state_cannot_rebootstrap_after_independent_authority_loss(
    tmp_path,
    monkeypatch,
):
    machine_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(machine_root.resolve()),
    )
    clock = FixedClock()
    path = tmp_path / "workspace" / "operator-confirmations.jsonl"

    _prepare_confirmed(path, clock=clock)
    assert path.read_bytes()
    assert path.with_name(f"{path.name}.head.json").is_file()
    assert machine_root.is_dir()

    shutil.rmtree(machine_root)

    with pytest.raises(
        SupervisedConfirmationIntegrityError,
        match="independent monotonic authority",
    ):
        SupervisedConfirmationAuthority(path, clock=clock)


def test_copied_nonempty_state_cannot_be_adopted_in_another_workspace(
    tmp_path,
    monkeypatch,
):
    machine_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(machine_root.resolve()),
    )
    clock = FixedClock()
    source_path = tmp_path / "workspace-a" / "operator-confirmations.jsonl"

    _prepare_confirmed(source_path, clock=clock)

    copied_path = tmp_path / "workspace-b" / source_path.name
    copied_path.parent.mkdir(parents=True)
    shutil.copy2(source_path, copied_path)
    shutil.copy2(
        source_path.with_name(f"{source_path.name}.head.json"),
        copied_path.with_name(f"{copied_path.name}.head.json"),
    )

    with pytest.raises(
        SupervisedConfirmationIntegrityError,
        match="independent monotonic authority",
    ):
        SupervisedConfirmationAuthority(copied_path, clock=clock)
