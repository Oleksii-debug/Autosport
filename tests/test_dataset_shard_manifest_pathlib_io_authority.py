from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
    verify_registered_dataset_shards,
)
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import ScientificRegistry


def test_registered_entry_rejects_pathlib_io_rebind_before_shard_open(
    tmp_path: Path,
) -> None:
    payload = b"pathlib-io-dispatch-must-stay-canonical"
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

    path_open = shard_manifest.Path.open
    path_globals = path_open.__globals__
    canonical_io = path_globals["io"]
    canonical_io_open = canonical_io.open
    hostile_calls: list[str] = []

    def hostile_open(*args, **kwargs):
        hostile_calls.append("called")
        return canonical_io_open(*args, **kwargs)

    path_globals["io"] = SimpleNamespace(open=hostile_open)
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="authority|canonical|dependency|dispatch|pathlib|open",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        path_globals["io"] = canonical_io

    assert hostile_calls == []

def test_registered_entry_rejects_os_path_owner_rebind_before_path_resolve(
    tmp_path: Path,
) -> None:
    payload = b"os-path-owner-must-stay-canonical"
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

    canonical_os_path = shard_manifest.os.path
    hostile_calls: list[str] = []

    def hostile_realpath(path, *args, **kwargs):
        hostile_calls.append("called")
        return canonical_os_path.realpath(path, *args, **kwargs)

    shard_manifest.os.path = SimpleNamespace(
        realpath=hostile_realpath,
        expanduser=canonical_os_path.expanduser,
    )
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="authority|canonical|dependency|owner|os.path",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-owner-rebind",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        shard_manifest.os.path = canonical_os_path

    assert hostile_calls == []

