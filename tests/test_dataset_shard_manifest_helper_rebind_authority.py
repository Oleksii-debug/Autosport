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
    verify_shard_files,
)
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import ScientificRegistry


def test_public_verifier_ignores_rebound_registered_manifest_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"helper-dispatch-falsifier"
    path = tmp_path / "shard.bin"
    path.write_bytes(payload)
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
    forged = SimpleNamespace(
        member_sha256=manifest.member_sha256,
        manifest_sha256=manifest.membership_manifest_sha256(),
        causal_cutoff="2026-09-01T00:59:59Z",
    )

    monkeypatch.setattr(
        shard_manifest,
        "_require_registered_manifest",
        lambda *_args, **_kwargs: forged,
    )

    with pytest.raises(
        DatasetShardManifestError,
        match="canonical|lineage|proof|authority",
    ):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )