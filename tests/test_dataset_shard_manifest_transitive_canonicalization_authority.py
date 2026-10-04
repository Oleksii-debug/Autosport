from __future__ import annotations

import hashlib

import pytest

import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifest,
    DatasetShardManifestError,
)


def test_verify_shard_files_rejects_transitive_manifest_canonicalizer_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    """A rebound canonicalizer must not erase descriptors from byte verification."""

    payload = b"descriptor-must-not-disappear"
    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="shard-0",
        relative_path="missing.bin",
        byte_size=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        event_start_utc="2026-01-01T00:00:00Z",
        event_end_utc="2026-01-01T00:00:01Z",
    )
    attacker_called = False

    def forged_canonical_shard_manifest(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return DatasetShardManifest(())

    monkeypatch.setattr(
        shard_manifest,
        "canonical_shard_manifest",
        forged_canonical_shard_manifest,
    )

    with pytest.raises(DatasetShardManifestError):
        shard_manifest.verify_shard_files(tmp_path, [descriptor])

    assert attacker_called is False
