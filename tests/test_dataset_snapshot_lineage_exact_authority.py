from __future__ import annotations

import pytest

from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
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
