from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autosport.monotonic_workspace_authority import MonotonicAuthorityConflictError
from autosport.point_in_time_authority import (
    HoldoutConsumptionError,
    HoldoutConsumptionLedger,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


BASE = datetime(2026, 1, 1, tzinfo=UTC)
SHA_A = "a" * 64


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        "dataset-1",
        SHA_A,
        "parlayapi:table_tennis",
        "license-evidence:v1",
        _iso(BASE),
        _iso(BASE + timedelta(minutes=10)),
        outcome_reveal_after=_iso(BASE + timedelta(hours=3)),
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, ScientificRegistry, DatasetSnapshot]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority_root = tmp_path / "machine-authority"
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    snapshot = _snapshot()
    registry.append(snapshot)
    return workspace, authority_root, registry, snapshot


def _ledger(
    workspace: Path,
    authority_root: Path,
    registry: ScientificRegistry,
) -> HoldoutConsumptionLedger:
    return HoldoutConsumptionLedger(
        workspace,
        scientific_registry=registry,
        monotonic_authority_root=authority_root,
    )


def _consume(
    ledger: HoldoutConsumptionLedger,
    snapshot: DatasetSnapshot,
    *,
    family: str,
    hour: int,
) -> None:
    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id=family,
        consumer_identity=f"evaluation-{family}",
        purpose="promotion-confirmation",
        consumed_at=BASE + timedelta(hours=hour),
    )


def test_complete_workspace_old_snapshot_restore_fails_closed(tmp_path: Path) -> None:
    workspace, authority_root, registry, snapshot = _fixture(tmp_path)
    ledger = _ledger(workspace, authority_root, registry)
    _consume(ledger, snapshot, family="family-1", hour=4)

    old_workspace = tmp_path / "workspace-generation-1"
    shutil.copytree(workspace, old_workspace)

    _consume(ledger, snapshot, family="family-2", hour=5)
    assert len(ledger.receipts()) == 2

    shutil.rmtree(workspace)
    shutil.copytree(old_workspace, workspace)

    reopened = _ledger(workspace, authority_root, registry)
    with pytest.raises(HoldoutConsumptionError, match="independent monotonic authority"):
        reopened.receipts()


def test_deleting_all_local_holdout_state_after_history_fails_closed(tmp_path: Path) -> None:
    workspace, authority_root, registry, snapshot = _fixture(tmp_path)
    ledger = _ledger(workspace, authority_root, registry)
    _consume(ledger, snapshot, family="family-1", hour=4)

    ledger.path.unlink()
    ledger.anchor_path.unlink()

    reopened = _ledger(workspace, authority_root, registry)
    with pytest.raises(HoldoutConsumptionError, match="independent monotonic authority"):
        reopened.receipts()


def test_complete_local_publish_recovers_pending_monotonic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, authority_root, registry, snapshot = _fixture(tmp_path)
    ledger = _ledger(workspace, authority_root, registry)

    def fail_commit(**_: object) -> None:
        raise MonotonicAuthorityConflictError("simulated crash before authority COMMIT")

    with monkeypatch.context() as patch:
        patch.setattr(ledger.monotonic_authority, "commit", fail_commit)
        with pytest.raises(HoldoutConsumptionError, match="commit was not completed"):
            _consume(ledger, snapshot, family="family-1", hour=4)

    # The complete local ledger+anchor pair is the exact PREPARE intent.  A fresh
    # process can prove its final receipt binding and deterministically finish the
    # same transaction instead of minting a new authority transition.
    reopened = _ledger(workspace, authority_root, registry)
    receipts = reopened.receipts()
    assert len(receipts) == 1
    assert receipts[0].confirmation_trial_family_id == "family-1"


def test_authority_root_inside_protected_workspace_is_rejected(tmp_path: Path) -> None:
    workspace, _, registry, _ = _fixture(tmp_path)

    with pytest.raises(HoldoutConsumptionError, match="cannot initialize"):
        HoldoutConsumptionLedger(
            workspace,
            scientific_registry=registry,
            monotonic_authority_root=workspace / "not-independent",
        )
