from __future__ import annotations

import importlib
from pathlib import Path
import sys
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
    assert reloaded._resolve_canonical_snapshot.__closure__ is None
    assert reloaded._require_exact_lineage_authority.__closure__ is None

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


def test_rewriting_legacy_trusted_snapshot_cannot_authorize_forged_record(
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

    monkeypatch.setattr(
        seal,
        "_TRUSTED_LINEAGE_NAMESPACE",
        {"record": forged_record},
        raising=False,
    )
    monkeypatch.setattr(DatasetSnapshotLineageAuthority, "record", forged_record)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="trusted DatasetSnapshotLineageAuthority class implementation changed: record",
    ):
        ledger.freshness_id(
            dataset_snapshot=snapshot,
            confirmation_trial_family_id="family-v1",
        )

    assert dispatched is False


def test_rewriting_legacy_pristine_delegate_does_not_redirect_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger, snapshot, _ = _canonical_holdout(tmp_path, monkeypatch)
    dispatched = False

    def forged_resolve(*args, **kwargs):
        nonlocal dispatched
        dispatched = True
        return snapshot

    monkeypatch.setattr(
        seal,
        "_PRISTINE_RESOLVE_CANONICAL_SNAPSHOT",
        forged_resolve,
        raising=False,
    )

    with pytest.raises(evidence.PointInTimeEvidenceError):
        ledger.freshness_id(
            dataset_snapshot=snapshot,
            confirmation_trial_family_id="family-v1",
        )

    assert dispatched is False


def test_removing_public_reload_finder_does_not_drop_seal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger, snapshot, _ = _canonical_holdout(tmp_path, monkeypatch)
    canonical = seal._CANONICAL_REPAIR_RELOAD_FINDER
    original_meta_path = list(sys.meta_path)
    try:
        sys.meta_path[:] = [finder for finder in sys.meta_path if finder is not canonical]
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
            raise AssertionError("backup reload seal must reject forged record")

        monkeypatch.setattr(DatasetSnapshotLineageAuthority, "record", forged_record)
        with pytest.raises(
            evidence.PointInTimeEvidenceError,
            match="trusted DatasetSnapshotLineageAuthority class implementation changed: record",
        ):
            ledger.freshness_id(
                dataset_snapshot=snapshot,
                confirmation_trial_family_id="family-v1",
            )
        assert dispatched is False
    finally:
        sys.meta_path[:] = original_meta_path
        seal._install_reload_finders()


def test_rebinding_seal_module_aliases_cannot_redirect_live_guard(
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

    monkeypatch.setattr(seal, "repair", SimpleNamespace(), raising=False)
    monkeypatch.setattr(seal, "DatasetSnapshotLineageAuthority", object, raising=False)
    monkeypatch.setattr(seal, "ScientificRegistry", object, raising=False)
    monkeypatch.setattr(seal, "sys", SimpleNamespace(meta_path=[]), raising=False)
    monkeypatch.setattr(seal, "importlib", SimpleNamespace(), raising=False)
    monkeypatch.setattr(DatasetSnapshotLineageAuthority, "record", forged_record)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="trusted DatasetSnapshotLineageAuthority class implementation changed: record",
    ):
        ledger.freshness_id(
            dataset_snapshot=snapshot,
            confirmation_trial_family_id="family-v1",
        )

    assert dispatched is False


def test_dispatch_authority_has_no_mutable_closure_trust_root() -> None:
    assert repair._require_exact_lineage_authority.__closure__ is None
    assert repair._resolve_canonical_snapshot.__closure__ is None
    assert seal._sealed_require_exact_lineage_authority.__closure__ is None
    assert seal._sealed_resolve_canonical_snapshot.__closure__ is None


def test_removing_both_seal_reload_hooks_cannot_restore_unsealed_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger, snapshot, _ = _canonical_holdout(tmp_path, monkeypatch)
    original_meta_path = list(sys.meta_path)
    try:
        sys.meta_path[:] = [
            finder
            for finder in sys.meta_path
            if type(finder).__module__ != seal.__name__
        ]
        reloaded = importlib.reload(repair)
        assert reloaded._require_exact_lineage_authority.__closure__ is None
        assert reloaded._resolve_canonical_snapshot.__closure__ is None

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
            match="trusted DatasetSnapshotLineageAuthority class implementation changed: record",
        ):
            ledger.freshness_id(
                dataset_snapshot=snapshot,
                confirmation_trial_family_id="family-v1",
            )
        assert dispatched is False
    finally:
        sys.meta_path[:] = original_meta_path
        seal._install_reload_finders()
        seal._install_seals()


def test_backup_reload_capability_is_not_a_public_module_global() -> None:
    assert not hasattr(seal, "_BACKUP_REPAIR_RELOAD_FINDER")
    assert not hasattr(seal, "_BackupRepairReloadFinder")
    assert not hasattr(seal, "_RepairReloadLoader")
