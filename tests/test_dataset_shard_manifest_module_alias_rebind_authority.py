from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.dataset_shard_manifest as shard_manifest_module
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
    verify_registered_dataset_shards,
    verify_shard_files,
)
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import ScientificRegistry


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_paired_module_alias_and_lineage_record_rebind_cannot_mint_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"paired-dispatch-rebind-falsifier"
    shard_path = tmp_path / "shard.bin"
    shard_path.write_bytes(payload)
    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="s0",
        relative_path="shard.bin",
        byte_size=len(payload),
        content_sha256=_sha(payload),
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

    def forged_record(self: DatasetSnapshotLineageAuthority, snapshot_id: str):
        assert self is authority
        assert snapshot_id == "snapshot-1"
        return forged

    monkeypatch.setattr(DatasetSnapshotLineageAuthority, "record", forged_record)
    monkeypatch.setattr(shard_manifest_module, "_LINEAGE_RECORD", forged_record)

    with pytest.raises(
        DatasetShardManifestError,
        match="authority|dispatch|canonical|lineage",
    ):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )