from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport import _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root
from autosport import _point_in_time_authority_runtime_repair as repair
from autosport import _point_in_time_class_dispatch_seal as seal
from autosport import point_in_time_evidence as evidence
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _canonical_holdout(
    tmp_path: Path,
    monkeypatch,
) -> tuple[evidence.HoldoutConsumptionLedger, DatasetSnapshot, Path]:
    product_root = (tmp_path / "product-machine-authority").resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )
    workspace = tmp_path / "holdout-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = ScientificRegistry.initialize_pristine(
        workspace / "scientific-registry.json"
    )
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        workspace / "dataset-snapshot-lineage.json",
        registry,
        authority_root=product_root,
    )
    ledger = evidence.HoldoutConsumptionLedger(
        workspace / "holdout.json",
        lineage_authority=lineage,
    )
    snapshot = DatasetSnapshot(
        dataset_snapshot_id="class-dispatch-forgery",
        manifest_sha256="a" * 64,
        source_identity="provider:class-dispatch-forgery",
        license_identity="terms:v1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )
    return ledger, snapshot, product_root


def test_holdout_rejects_class_level_lineage_record_replacement_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger, snapshot, _ = _canonical_holdout(tmp_path, monkeypatch)
    dispatched = False

    def forged_record(self, snapshot_id: str):
        nonlocal dispatched
        dispatched = True
        return SimpleNamespace(
            snapshot_id=snapshot_id,
            manifest_sha256=snapshot.manifest_sha256,
            source_identity=snapshot.source_identity,
            license_identity=snapshot.license_identity,
            causal_cutoff=snapshot.causal_cutoff,
            available_at=snapshot.available_at_utc,
        )

    monkeypatch.setattr(DatasetSnapshotLineageAuthority, "record", forged_record)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match=(
            "trusted DatasetSnapshotLineageAuthority class implementation changed: record"
        ),
    ):
        ledger.freshness_id(
            dataset_snapshot=snapshot,
            confirmation_trial_family_id="family-v1",
        )

    assert dispatched is False


def test_holdout_rejects_class_level_registry_get_replacement_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger, snapshot, _ = _canonical_holdout(tmp_path, monkeypatch)
    dispatched = False

    def forged_get(self, record_type: str, record_id: str):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("caller-replaced ScientificRegistry.get must never execute")

    monkeypatch.setattr(ScientificRegistry, "get", forged_get)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="trusted ScientificRegistry class implementation changed: get",
    ):
        ledger.access_id(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
        )

    assert dispatched is False


def test_runtime_repair_reload_reinstalls_class_dispatch_seal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger, snapshot, _ = _canonical_holdout(tmp_path, monkeypatch)

    reloaded = importlib.reload(repair)
    assert reloaded._resolve_canonical_snapshot is seal._sealed_resolve_canonical_snapshot
    assert (
        reloaded._require_exact_lineage_authority
        is seal._sealed_require_exact_lineage_authority
    )

    dispatched = False

    def forged_record(self, snapshot_id: str):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("repair reload must not restore mutable class dispatch")

    monkeypatch.setattr(DatasetSnapshotLineageAuthority, "record", forged_record)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match=(
            "trusted DatasetSnapshotLineageAuthority class implementation changed: record"
        ),
    ):
        ledger.assert_unused(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-v1",
            confirmation_trial_family_id="family-v1",
        )

    assert dispatched is False
