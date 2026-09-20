from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
        "dataset-rollback-guard",
        SHA_A,
        "lawful-provider:fixture",
        "license-evidence:v1",
        _iso(BASE),
        _iso(BASE + timedelta(minutes=10)),
        outcome_reveal_after=_iso(BASE + timedelta(minutes=20)),
    )


def _ledger(tmp_path, monkeypatch):
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    snapshot = _snapshot()
    registry.append(snapshot)
    workspace = tmp_path / "holdout"
    workspace.mkdir()
    return (
        HoldoutConsumptionLedger(workspace, scientific_registry=registry),
        registry,
        snapshot,
        workspace,
    )


def test_paired_valid_ledger_anchor_rollback_cannot_erase_later_consumption(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, registry, snapshot, workspace = _ledger(tmp_path, monkeypatch)

    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-a",
        consumer_identity="research-worker",
        purpose="confirmation",
        consumed_at=BASE + timedelta(minutes=30),
    )
    old_ledger = ledger.path.read_bytes()
    old_anchor = ledger.anchor_path.read_bytes()

    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-b",
        consumer_identity="research-worker",
        purpose="confirmation",
        consumed_at=BASE + timedelta(minutes=31),
    )

    # Both restored files are a mutually consistent, previously valid pair.
    # The independent shared machine authority must still reject the rollback.
    ledger.path.write_bytes(old_ledger)
    ledger.anchor_path.write_bytes(old_anchor)

    restarted = HoldoutConsumptionLedger(
        workspace,
        scientific_registry=registry,
    )
    with pytest.raises(HoldoutConsumptionError, match="rollback|monotonic"):
        restarted.receipts()


def test_complete_holdout_workspace_state_deletion_cannot_rebootstrap_pristine(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, registry, snapshot, workspace = _ledger(tmp_path, monkeypatch)

    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-delete",
        confirmation_trial_family_id="family-delete",
        consumer_identity="research-worker",
        purpose="confirmation",
        consumed_at=BASE + timedelta(minutes=30),
    )
    ledger.path.unlink()
    ledger.anchor_path.unlink()

    restarted = HoldoutConsumptionLedger(
        workspace,
        scientific_registry=registry,
    )
    with pytest.raises(HoldoutConsumptionError, match="rollback|deletion|monotonic"):
        restarted.receipts()
