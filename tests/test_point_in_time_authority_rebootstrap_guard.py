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


def test_paired_valid_ledger_anchor_rollback_cannot_erase_later_consumption(
    tmp_path,
) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    snapshot = _snapshot()
    registry.append(snapshot)

    workspace = tmp_path / "holdout"
    workspace.mkdir()
    ledger = HoldoutConsumptionLedger(
        workspace,
        scientific_registry=registry,
    )

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

    second = ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-b",
        consumer_identity="research-worker",
        purpose="confirmation",
        consumed_at=BASE + timedelta(minutes=31),
    )
    assert (ledger.monotonic_dir / f"{second.holdout_access_id}.json").exists()

    # Both restored files are a mutually consistent, previously valid pair.
    # The independent immutable marker must still make this rollback fail closed.
    ledger.path.write_bytes(old_ledger)
    ledger.anchor_path.write_bytes(old_anchor)

    restarted = HoldoutConsumptionLedger(
        workspace,
        scientific_registry=registry,
    )
    with pytest.raises(HoldoutConsumptionError, match="rollback"):
        restarted.receipts()
