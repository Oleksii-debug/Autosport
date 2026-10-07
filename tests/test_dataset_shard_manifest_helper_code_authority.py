from __future__ import annotations

import builtins
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


def test_public_verifier_rejects_in_place_registered_manifest_helper_code_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"helper-code-dispatch-falsifier"
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
    canonical = shard_manifest._require_registered_manifest

    def forged(
        authority,
        *,
        snapshot_id,
        manifest,
        _authority_type=None,
        _record_reader=None,
        _manifest_digest=None,
    ):
        del (
            authority,
            snapshot_id,
            manifest,
            _authority_type,
            _record_reader,
            _manifest_digest,
        )
        return SimpleNamespace(snapshot_id="forged")

    monkeypatch.setattr(canonical, "__code__", forged.__code__)

    with pytest.raises(
        DatasetShardManifestError,
        match="canonical|lineage|proof|authority|executable|dispatch",
    ):
        verify_registered_dataset_shards(
            object(),
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )


def test_public_verifier_rejects_builtin_getattr_code_laundering_before_hostile_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="s0",
        relative_path="missing.bin",
        byte_size=7,
        content_sha256="0" * 64,
        event_start_utc="2026-09-01T00:00:00Z",
        event_end_utc="2026-09-01T00:59:59Z",
    )
    helper = shard_manifest._verify_descriptor_bytes
    original_code = helper.__code__
    original_getattr = builtins.getattr
    hostile_calls: list[str] = []

    monkeypatch.setattr(
        shard_manifest,
        "_WAVE_M_TEST_ORIGINAL_VERIFY_CODE",
        original_code,
        raising=False,
    )
    monkeypatch.setattr(
        shard_manifest,
        "_WAVE_M_TEST_ORIGINAL_GETATTR",
        original_getattr,
        raising=False,
    )
    monkeypatch.setattr(
        shard_manifest,
        "_WAVE_M_TEST_BUILTINS",
        builtins,
        raising=False,
    )
    monkeypatch.setattr(
        shard_manifest,
        "_WAVE_M_TEST_HOSTILE_CALLS",
        hostile_calls,
        raising=False,
    )

    def forged(root, candidate, *, capture_bytes=False):
        del root, candidate, capture_bytes
        _WAVE_M_TEST_HOSTILE_CALLS.append("called")
        _verify_descriptor_bytes.__code__ = _WAVE_M_TEST_ORIGINAL_VERIFY_CODE
        _WAVE_M_TEST_BUILTINS.getattr = _WAVE_M_TEST_ORIGINAL_GETATTR
        return None

    def deceptive_getattr(obj, name, *default):
        if obj is helper and name == "__code__":
            return original_code
        return original_getattr(obj, name, *default)

    monkeypatch.setattr(helper, "__code__", forged.__code__)
    monkeypatch.setattr(builtins, "getattr", deceptive_getattr)

    with pytest.raises(
        DatasetShardManifestError,
        match="canonical|builtin|authority|executable|dispatch",
    ):
        verify_registered_dataset_shards(
            object(),
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )

    assert hostile_calls == []
