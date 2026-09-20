from __future__ import annotations

import pytest

from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.point_in_time_evidence import (
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
