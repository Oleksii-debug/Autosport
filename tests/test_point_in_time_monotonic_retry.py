from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import autosport.point_in_time_authority as point_in_time
from autosport.point_in_time_authority import HoldoutConsumptionLedger
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


BASE = datetime(2026, 1, 1, tzinfo=UTC)
SHA_A = "a" * 64


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _fixture(tmp_path: Path) -> tuple[Path, Path, ScientificRegistry, DatasetSnapshot]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority_root = tmp_path / "machine-authority"
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    snapshot = DatasetSnapshot(
        "dataset-1",
        SHA_A,
        "parlayapi:table_tennis",
        "license-evidence:v1",
        _iso(BASE),
        _iso(BASE + timedelta(minutes=10)),
        outcome_reveal_after=_iso(BASE + timedelta(hours=3)),
    )
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


def _consume(ledger: HoldoutConsumptionLedger, snapshot: DatasetSnapshot) -> None:
    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-family-1",
        purpose="promotion-confirmation",
        consumed_at=BASE + timedelta(hours=4),
    )


def test_exact_retry_after_aborted_prepare_uses_fresh_authority_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, authority_root, registry, snapshot = _fixture(tmp_path)
    ledger = _ledger(workspace, authority_root, registry)
    real_atomic_write_json = point_in_time.atomic_write_json
    calls = 0

    def fail_before_local_publish(path: Path, payload: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated crash after PREPARE before local publish")
        real_atomic_write_json(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(point_in_time, "atomic_write_json", fail_before_local_publish)
        with pytest.raises(OSError, match="after PREPARE"):
            _consume(ledger, snapshot)

    # Reopening sees pristine local state and deterministically ABORTs the orphaned
    # PREPARE.  An exact retry has the same semantic receipt digest, but it must use
    # a fresh authority transaction rather than trying to COMMIT the terminal ABORT.
    reopened = _ledger(workspace, authority_root, registry)
    _consume(reopened, snapshot)

    receipts = reopened.receipts()
    assert len(receipts) == 1
    assert receipts[0].confirmation_trial_family_id == "family-1"

    history = reopened.monotonic_authority.read_history()
    assert [record.phase.value for record in history] == [
        "PREPARE",
        "ABORT",
        "PREPARE",
        "COMMIT",
    ]
    assert history[0].tx_id != history[2].tx_id


def test_restart_repairs_exact_pending_ledger_before_anchor_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, authority_root, registry, snapshot = _fixture(tmp_path)
    ledger = _ledger(workspace, authority_root, registry)
    real_atomic_write_json = point_in_time.atomic_write_json
    calls = 0

    def fail_anchor_publish(path: Path, payload: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash after ledger before anchor")
        real_atomic_write_json(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(point_in_time, "atomic_write_json", fail_anchor_publish)
        with pytest.raises(OSError, match="before anchor"):
            _consume(ledger, snapshot)

    assert ledger.path.exists()
    assert not ledger.anchor_path.exists()

    # The external PREPARE commits to the exact local ledger digest + final receipt
    # binding, so restart may reconstruct only the missing first-generation anchor
    # and then finish that same prepared transaction.  It must not mint new state.
    reopened = _ledger(workspace, authority_root, registry)
    receipts = reopened.receipts()
    assert len(receipts) == 1
    assert receipts[0].confirmation_trial_family_id == "family-1"

    history = reopened.monotonic_authority.read_history()
    assert [record.phase.value for record in history] == ["PREPARE", "COMMIT"]
