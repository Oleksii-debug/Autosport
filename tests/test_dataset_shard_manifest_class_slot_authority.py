from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import autosport._dataset_shard_manifest_authority_guard  # noqa: F401
import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
    verify_registered_dataset_shards,
)


def _descriptor() -> DatasetShardDescriptor:
    return DatasetShardDescriptor(
        ordinal=0,
        shard_id="s0",
        relative_path="missing.bin",
        byte_size=7,
        content_sha256="0" * 64,
        event_start_utc="2026-09-01T00:00:00Z",
        event_end_utc="2026-09-01T00:59:59Z",
    )


def test_registered_entry_rejects_descriptor_getattribute_slot_replacement_before_dispatch(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    original_slot = DatasetShardDescriptor.__dict__.get("__getattribute__")
    inherited_getattribute = DatasetShardDescriptor.__getattribute__
    hostile_calls: list[str] = []

    def hostile_getattribute(self: DatasetShardDescriptor, name: str):
        hostile_calls.append(name)
        return inherited_getattribute(self, name)

    type.__setattr__(DatasetShardDescriptor, "__getattribute__", hostile_getattribute)
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="class dispatch|canonical|authority|executable",
        ):
            verify_registered_dataset_shards(
                object(),
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        if original_slot is None:
            type.__delattr__(DatasetShardDescriptor, "__getattribute__")
        else:
            type.__setattr__(
                DatasetShardDescriptor,
                "__getattribute__",
                original_slot,
            )

    assert hostile_calls == []


def test_registered_entry_rejects_descriptor_semantic_helper_rebind_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = _descriptor()
    hostile_calls: list[str] = []

    def forged_sha256(value: object, name: str) -> str:
        del value, name
        hostile_calls.append("called")
        return "0" * 64

    monkeypatch.setattr(shard_manifest, "_sha256", forged_sha256)

    with pytest.raises(
        DatasetShardManifestError,
        match="module authority|canonical|authority|executable",
    ):
        verify_registered_dataset_shards(
            object(),
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )

    assert hostile_calls == []


def test_registered_entry_rejects_hashlib_sha256_rebind_before_file_hash_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"dependency-dispatch-must-stay-canonical"
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
    canonical_sha256 = shard_manifest.hashlib.sha256
    hostile_calls: list[str] = []

    def hostile_sha256(*args, **kwargs):
        hostile_calls.append("called")
        return canonical_sha256(*args, **kwargs)

    monkeypatch.setattr(shard_manifest.hashlib, "sha256", hostile_sha256)

    with pytest.raises(
        DatasetShardManifestError,
        match="dependency dispatch changed: hashlib.sha256",
    ):
        verify_registered_dataset_shards(
            object(),
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )

    assert hostile_calls == []


def test_registered_entry_rejects_path_constructor_slot_replacement_before_detach(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    path_type = shard_manifest.Path
    original_slot = path_type.__dict__.get("__new__")
    inherited_new = path_type.__new__
    hostile_calls: list[str] = []

    def hostile_new(cls, *args, **kwargs):
        hostile_calls.append("called")
        return inherited_new(cls, *args, **kwargs)

    type.__setattr__(path_type, "__new__", hostile_new)
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="dependency dispatch changed: Path.__new__",
        ):
            verify_registered_dataset_shards(
                object(),
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        if original_slot is None:
            type.__delattr__(path_type, "__new__")
        else:
            type.__setattr__(path_type, "__new__", original_slot)

    assert hostile_calls == []
