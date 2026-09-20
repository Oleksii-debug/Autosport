from __future__ import annotations

import os
from pathlib import Path
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


def test_holdout_atomic_publication_fails_when_directory_sync_is_unavailable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "holdout.json"
    real_open = os.open

    def fail_only_directory_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            raise OSError("directory fsync unavailable")
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
