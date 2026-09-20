from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from autosport import point_in_time_evidence as evidence
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.point_in_time_evidence import (
    EvidenceLedgerCorruptError,
    PointInTimeEvidenceError,
    PointInTimeFeatureAuthority,
)
from autosport.scientific_registry import ScientificRegistry


def test_feature_authority_rejects_lineage_subclass_before_authority_dispatch() -> None:
    dispatched = False

    class ForgedLineageAuthority(DatasetSnapshotLineageAuthority):
        def record(self, snapshot_id: str):
            nonlocal dispatched
            dispatched = True
            raise AssertionError("caller-polymorphic record() must never execute")

    forged = object.__new__(ForgedLineageAuthority)

    with pytest.raises(
        PointInTimeEvidenceError,
        match="lineage_authority must be an exact DatasetSnapshotLineageAuthority",
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=forged,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )

    assert dispatched is False


def test_feature_authority_rejects_nested_registry_subclass_before_dispatch() -> None:
    dispatched = False

    class ForgedRegistry(ScientificRegistry):
        def get(self, record_type: str, record_id: str):
            nonlocal dispatched
            dispatched = True
            raise AssertionError("caller-polymorphic registry.get() must never execute")

    lineage = object.__new__(DatasetSnapshotLineageAuthority)
    lineage.registry = object.__new__(ForgedRegistry)

    with pytest.raises(
        PointInTimeEvidenceError,
        match="lineage_authority.registry must be an exact ScientificRegistry",
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )

    assert dispatched is False


def test_feature_authority_rejects_exact_lineage_instance_method_shadow() -> None:
    dispatched = False

    def forged_record(snapshot_id: str):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("caller-shadowed record() must never execute")

    lineage = object.__new__(DatasetSnapshotLineageAuthority)
    lineage.registry = object.__new__(ScientificRegistry)
    lineage.record = forged_record

    with pytest.raises(
        PointInTimeEvidenceError,
        match="lineage_authority shadows trusted concrete authority method: record",
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )

    assert dispatched is False


def test_feature_authority_rejects_exact_registry_instance_method_shadow() -> None:
    dispatched = False

    def forged_get(record_type: str, record_id: str):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("caller-shadowed registry.get() must never execute")

    registry = object.__new__(ScientificRegistry)
    registry.get = forged_get
    lineage = object.__new__(DatasetSnapshotLineageAuthority)
    lineage.registry = registry

    with pytest.raises(
        PointInTimeEvidenceError,
        match=(
            "lineage_authority.registry shadows trusted concrete authority method: get"
        ),
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )

    assert dispatched is False


def test_point_in_time_module_reload_cannot_restore_legacy_positive_bind() -> None:
    script = r'''
import importlib
import autosport.point_in_time_evidence as evidence
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import DatasetSnapshot, FeatureSet

reloaded = importlib.reload(evidence)
snapshot = DatasetSnapshot(
    dataset_snapshot_id="reload-snapshot",
    manifest_sha256="a" * 64,
    source_identity="provider:reload-falsifier",
    license_identity="terms:v1",
    causal_cutoff="2026-09-20T10:00:00Z",
    available_at_utc="2026-09-20T10:01:00Z",
)
feature_set = FeatureSet(
    feature_set_id="reload.feature.v1",
    version="v1",
    definition_sha256="b" * 64,
    source_sha256="c" * 64,
    available_at_utc="2026-09-20T10:01:30Z",
)

try:
    reloaded.PointInTimeFeatureAuthority.bind(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_payload_sha256="d" * 64,
        decision_cutoff_utc="2099-01-01T00:00:00Z",
    )
except TypeError as exc:
    assert "lineage_authority" in str(exc)
else:
    raise AssertionError("reload restored the legacy caller-mintable four-argument bind")

class ForgedLineageAuthority(DatasetSnapshotLineageAuthority):
    pass

forged = object.__new__(ForgedLineageAuthority)
try:
    reloaded.PointInTimeFeatureAuthority.bind(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_provenance=object(),
        lineage_authority=forged,
        decision_cutoff_utc="2099-01-01T00:00:00Z",
    )
except reloaded.PointInTimeEvidenceError as exc:
    assert "lineage_authority must be an exact DatasetSnapshotLineageAuthority" in str(exc)
else:
    raise AssertionError("reload removed the exact lineage capability fence")
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="Windows intentionally has no directory fsync contract")
def test_holdout_atomic_publication_fails_when_directory_open_for_sync_is_unavailable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "holdout.json"
    real_open = os.open

    def fail_only_directory_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            raise OSError("directory open unavailable")
        return real_open(path, flags, *args, **kwargs)

    with patch(
        "autosport._point_in_time_authority_runtime_repair.os.open",
        side_effect=fail_only_directory_open,
    ):
        with pytest.raises(
            EvidenceLedgerCorruptError,
            match="cannot open holdout ledger directory for durability",
        ):
            evidence._atomic_write_json(state_path, {"schema_version": 1})


@pytest.mark.skipif(os.name == "nt", reason="Windows intentionally has no directory fsync contract")
def test_holdout_atomic_publication_fails_when_directory_fsync_fails(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "holdout.json"
    real_fsync = os.fsync

    def fail_only_directory_fsync(descriptor: int):
        target = Path(f"/proc/self/fd/{descriptor}")
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            return real_fsync(descriptor)
        if resolved == tmp_path:
            raise OSError("directory fsync unavailable")
        return real_fsync(descriptor)

    with patch(
        "autosport._point_in_time_authority_runtime_repair.os.fsync",
        side_effect=fail_only_directory_fsync,
    ):
        with pytest.raises(
            EvidenceLedgerCorruptError,
            match="cannot fsync holdout ledger directory for durability",
        ):
            evidence._atomic_write_json(state_path, {"schema_version": 1})