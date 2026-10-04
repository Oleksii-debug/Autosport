from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
    verify_registered_dataset_shards,
    verify_shard_files,
)
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import ScientificRegistry


_FORGED_RECORD_FROM_RAW_CALLS = 0


def _forged_record_from_raw(raw: object):
    global _FORGED_RECORD_FROM_RAW_CALLS
    _FORGED_RECORD_FROM_RAW_CALLS += 1
    return raw


def _fixture(tmp_path: Path):
    payload = b"lineage-reader-dispatch-falsifier"
    (tmp_path / "shard.bin").write_bytes(payload)
    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="s0",
        relative_path="shard.bin",
        byte_size=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        event_start_utc="2026-09-01T00:00:00Z",
        event_end_utc="2026-09-01T00:59:59Z",
    )
    manifest = verify_shard_files(tmp_path, (descriptor,))
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    authority = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=tmp_path.with_name(
            tmp_path.name + "-dataset-lineage-machine-state"
        ),
    )
    return descriptor, manifest, authority


def _forged_record(manifest):
    return SimpleNamespace(
        snapshot_id="snapshot-1",
        member_sha256=manifest.member_sha256,
        manifest_sha256=manifest.membership_manifest_sha256(),
        causal_cutoff="2026-09-01T00:59:59Z",
    )


def test_rebound_lineage_read_and_verify_cannot_mint_registered_shard_authority(
    tmp_path: Path,
) -> None:
    descriptor, manifest, authority = _fixture(tmp_path)
    forged = _forged_record(manifest)
    original = DatasetSnapshotLineageAuthority.__dict__["_read_and_verify"]
    hostile_calls = 0

    def hostile_read_and_verify(self: DatasetSnapshotLineageAuthority):
        nonlocal hostile_calls
        assert self is authority
        hostile_calls += 1
        return (forged,)

    type.__setattr__(
        DatasetSnapshotLineageAuthority,
        "_read_and_verify",
        hostile_read_and_verify,
    )
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="authority|canonical|dispatch|lineage|executable",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        type.__setattr__(
            DatasetSnapshotLineageAuthority,
            "_read_and_verify",
            original,
        )

    assert hostile_calls == 0


def test_lineage_reader_instance_shadow_cannot_mint_registered_shard_authority(
    tmp_path: Path,
) -> None:
    descriptor, manifest, authority = _fixture(tmp_path)
    forged = _forged_record(manifest)
    hostile_calls = 0

    def hostile_read_and_verify():
        nonlocal hostile_calls
        hostile_calls += 1
        return (forged,)

    authority.__dict__["_read_and_verify"] = hostile_read_and_verify
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="authority|canonical|dispatch|lineage|shadow",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        authority.__dict__.pop("_read_and_verify", None)

    assert hostile_calls == 0


def test_in_place_lineage_staticmethod_code_swap_is_rejected_before_dispatch(
    tmp_path: Path,
) -> None:
    global _FORGED_RECORD_FROM_RAW_CALLS
    descriptor, _manifest, authority = _fixture(tmp_path)
    descriptor_slot = DatasetSnapshotLineageAuthority.__dict__["_record_from_raw"]
    canonical_record_from_raw = descriptor_slot.__func__
    original_code = canonical_record_from_raw.__code__
    _FORGED_RECORD_FROM_RAW_CALLS = 0

    try:
        canonical_record_from_raw.__code__ = _forged_record_from_raw.__code__
        with pytest.raises(
            DatasetShardManifestError,
            match="class executable changed: DatasetSnapshotLineageAuthority._record_from_raw",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        canonical_record_from_raw.__code__ = original_code

    assert _FORGED_RECORD_FROM_RAW_CALLS == 0
