from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.dataset_snapshot_lineage as lineage_module
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
    verify_registered_dataset_shards,
    verify_shard_files,
)
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import (
    DatasetSnapshot,
    ScientificRegistry,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path):
    payload = b"caller-mint-falsifier"
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
    return descriptor, manifest, registry, authority


def _forged_record(self, snapshot_id: str):
    del self, snapshot_id
    globals()["_FORGED_RECORD_CALLS"] += 1
    return globals()["_FORGED_RECORD_RESULT"]


def _forged_membership_manifest_sha256(member_sha256):
    del member_sha256
    globals()["_FORGED_MANIFEST_CALLS"] += 1
    return globals()["_FORGED_MANIFEST_RESULT"]


def test_rebound_lineage_record_dispatch_cannot_mint_registered_shard_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, manifest, _registry, authority = _fixture(tmp_path)

    # No canonical snapshot or lineage record is registered. A rebound class method
    # must therefore never be able to fabricate the positive record consumed by the
    # shard verifier.
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


def test_in_place_lineage_record_code_swap_is_rejected_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, manifest, _registry, authority = _fixture(tmp_path)
    forged = SimpleNamespace(
        member_sha256=manifest.member_sha256,
        manifest_sha256=manifest.membership_manifest_sha256(),
        causal_cutoff="2026-09-01T00:59:59Z",
    )
    canonical_record = DatasetSnapshotLineageAuthority.record
    original_code = canonical_record.__code__
    monkeypatch.setattr(lineage_module, "_FORGED_RECORD_CALLS", 0, raising=False)
    monkeypatch.setattr(lineage_module, "_FORGED_RECORD_RESULT", forged, raising=False)

    try:
        canonical_record.__code__ = _forged_record.__code__
        with pytest.raises(
            DatasetShardManifestError,
            match="authority|dispatch|canonical|lineage|executable",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        canonical_record.__code__ = original_code

    assert lineage_module._FORGED_RECORD_CALLS == 0


def test_in_place_membership_digest_code_swap_is_rejected_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, manifest, registry, authority = _fixture(tmp_path)
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
    record = authority.register(
        snapshot_id="snapshot-1",
        member_sha256=manifest.member_sha256,
    )

    canonical_digest = lineage_module.membership_manifest_sha256
    original_code = canonical_digest.__code__
    monkeypatch.setattr(lineage_module, "_FORGED_MANIFEST_CALLS", 0, raising=False)
    monkeypatch.setattr(
        lineage_module,
        "_FORGED_MANIFEST_RESULT",
        record.manifest_sha256,
        raising=False,
    )

    try:
        canonical_digest.__code__ = _forged_membership_manifest_sha256.__code__
        with pytest.raises(
            DatasetShardManifestError,
            match="authority|dispatch|canonical|lineage|executable|manifest",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        canonical_digest.__code__ = original_code

    assert lineage_module._FORGED_MANIFEST_CALLS == 0