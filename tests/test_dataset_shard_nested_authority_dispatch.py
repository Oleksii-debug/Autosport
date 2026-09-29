from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
    verify_registered_dataset_shards,
    verify_shard_files,
)
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _fixture(tmp_path: Path):
    payload = b"nested-lineage-authority-falsifier"
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
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="snapshot-1",
            manifest_sha256=manifest.membership_manifest_sha256(),
            source_identity="provider:test-shards",
            license_identity="license:test-v1",
            causal_cutoff="2026-09-01T00:59:59Z",
            available_at_utc="2026-09-02T00:00:00Z",
        )
    )
    authority = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=tmp_path.with_name(
            tmp_path.name + "-dataset-lineage-machine-state"
        ),
    )
    authority.register(
        snapshot_id="snapshot-1",
        member_sha256=manifest.member_sha256,
    )
    return descriptor, registry, authority


def test_registry_instance_shadow_cannot_enter_registered_shard_lineage_read(
    tmp_path: Path,
) -> None:
    descriptor, registry, authority = _fixture(tmp_path)
    hostile_calls = 0

    def hostile_get(*_args, **_kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        raise AssertionError("hostile registry dispatch executed")

    registry.__dict__["get"] = hostile_get
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="nested ScientificRegistry instance dispatch shadowed",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        registry.__dict__.pop("get", None)

    assert hostile_calls == 0


def test_monotonic_instance_shadow_cannot_enter_registered_shard_lineage_read(
    tmp_path: Path,
) -> None:
    descriptor, _registry, authority = _fixture(tmp_path)
    hostile_calls = 0

    def hostile_recover(*_args, **_kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        raise AssertionError("hostile monotonic dispatch executed")

    monotonic = authority.monotonic_authority
    monotonic.__dict__["recover"] = hostile_recover
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="nested MonotonicWorkspaceAuthority instance dispatch shadowed",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        monotonic.__dict__.pop("recover", None)

    assert hostile_calls == 0
