from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
    verify_registered_dataset_shards,
    verify_shard_files,
)
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    DatasetSnapshotLineageRecord,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _descriptor(shard_id: str, path: str, payload: bytes) -> DatasetShardDescriptor:
    return DatasetShardDescriptor(
        ordinal=0,
        shard_id=shard_id,
        relative_path=path,
        byte_size=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        event_start_utc="2026-09-01T00:00:00Z",
        event_end_utc="2026-09-01T00:59:59Z",
    )


def test_lineage_record_getattribute_cannot_relabel_registered_manifest(
    tmp_path: Path,
) -> None:
    registered_payload = b"registered-lineage"
    forged_payload = b"different-caller-manifest"
    (tmp_path / "registered.bin").write_bytes(registered_payload)
    (tmp_path / "forged.bin").write_bytes(forged_payload)
    registered_descriptor = _descriptor(
        "registered",
        "registered.bin",
        registered_payload,
    )
    forged_descriptor = _descriptor("forged", "forged.bin", forged_payload)
    registered_manifest = verify_shard_files(tmp_path, (registered_descriptor,))
    forged_manifest = verify_shard_files(tmp_path, (forged_descriptor,))

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="snapshot-1",
            manifest_sha256=registered_manifest.membership_manifest_sha256(),
            source_identity="provider:test-shards",
            license_identity="license:test-v1",
            causal_cutoff="2026-09-01T00:59:59Z",
            available_at_utc="2026-09-02T00:00:00Z",
        )
    )
    authority = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
    )
    authority.register(
        snapshot_id="snapshot-1",
        member_sha256=registered_manifest.member_sha256,
    )

    original = DatasetSnapshotLineageRecord.__dict__.get("__getattribute__")
    hostile_calls = 0

    def hostile_getattribute(self: DatasetSnapshotLineageRecord, name: str):
        nonlocal hostile_calls
        caller = sys._getframe(1).f_code.co_name
        if caller == "_require_registered_manifest":
            hostile_calls += 1
            if name == "member_sha256":
                return forged_manifest.member_sha256
            if name == "manifest_sha256":
                return forged_manifest.membership_manifest_sha256()
            if name == "causal_cutoff":
                return "2026-09-01T00:59:59Z"
        return object.__getattribute__(self, name)

    type.__setattr__(DatasetSnapshotLineageRecord, "__getattribute__", hostile_getattribute)
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="authority|canonical|dispatch|lineage|record|commitments",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(forged_descriptor,),
            )
    finally:
        if original is None:
            type.__delattr__(DatasetSnapshotLineageRecord, "__getattribute__")
        else:
            type.__setattr__(
                DatasetSnapshotLineageRecord,
                "__getattribute__",
                original,
            )

    assert hostile_calls == 0
