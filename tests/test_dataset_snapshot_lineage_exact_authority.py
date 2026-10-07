from __future__ import annotations

import pytest

from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    DatasetSnapshotLineageRecord,
    membership_manifest_sha256,
)
from autosport.scientific_registry import ScientificRegistry


class _RegistrySubclass(ScientificRegistry):
    pass


class _TupleSubclass(tuple):
    def __iter__(self):
        raise AssertionError("tuple subclass iteration must not execute")


def test_membership_manifest_rejects_tuple_subclass_before_iteration() -> None:
    hostile = _TupleSubclass(("a" * 64,))

    with pytest.raises(ValueError, match="exact tuple"):
        membership_manifest_sha256(hostile)  # type: ignore[arg-type]


def test_authority_constructor_rejects_registry_subclass_before_member_access(tmp_path) -> None:
    hostile = object.__new__(_RegistrySubclass)

    with pytest.raises(ValueError, match="exact ScientificRegistry"):
        DatasetSnapshotLineageAuthority(
            tmp_path / "lineage.json",
            hostile,
            authority_root=tmp_path / "machine",
        )


def test_pristine_initializer_rejects_registry_subclass_before_filesystem_mutation(
    tmp_path,
) -> None:
    hostile = object.__new__(_RegistrySubclass)
    lineage = tmp_path / "lineage.json"

    with pytest.raises(ValueError, match="exact ScientificRegistry"):
        DatasetSnapshotLineageAuthority.initialize_pristine(
            lineage,
            hostile,
            authority_root=tmp_path / "machine",
        )

    assert not lineage.exists()


@pytest.mark.parametrize("value", ("snapshot\n1", "snapshot\t1", "snapshot\r1", "snapshot\x7f1"))
def test_dataset_lineage_identity_rejects_control_aliases(value: str) -> None:
    with pytest.raises(ValueError, match="canonical string"):
        DatasetSnapshotLineageRecord(
            snapshot_id=value,
            dataset_record_sha256="a" * 64,
            manifest_sha256="b" * 64,
            source_identity="source-1",
            license_identity="license-1",
            causal_cutoff="2026-10-07T00:00:00Z",
            available_at="2026-10-07T00:00:00Z",
            proof_registered_at=None,
            member_sha256=("c" * 64,),
            parent_snapshot_id=None,
            parent_dataset_record_sha256=None,
            parent_proof_sha256=None,
            proof_sha256="d" * 64,
        )
