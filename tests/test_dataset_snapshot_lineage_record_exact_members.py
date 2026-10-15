from __future__ import annotations

import pytest

from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageRecord


class TupleSubclass(tuple):
    def __iter__(self):
        raise AssertionError("tuple subclass iteration must not execute")


def test_record_rejects_tuple_subclass_before_iteration() -> None:
    with pytest.raises(ValueError, match="exact tuple"):
        DatasetSnapshotLineageRecord(
            snapshot_id="snapshot-1",
            dataset_record_sha256="b" * 64,
            manifest_sha256="c" * 64,
            source_identity="provider:source-a",
            license_identity="license:v1",
            causal_cutoff="2026-10-01T00:00:00Z",
            available_at="2026-10-01T00:01:00Z",
            proof_registered_at=None,
            member_sha256=TupleSubclass(("a" * 64,)),
            parent_snapshot_id=None,
            parent_dataset_record_sha256=None,
            parent_proof_sha256=None,
            proof_sha256="d" * 64,
        )
