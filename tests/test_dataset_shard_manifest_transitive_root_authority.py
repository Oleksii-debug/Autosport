from __future__ import annotations

import hashlib

import pytest

import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
)


def test_verify_shard_files_rejects_transitive_root_resolver_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    """A rebound root resolver must not redirect canonical shard verification."""

    requested_root = tmp_path / "requested"
    requested_root.mkdir()
    substitute_root = tmp_path / "substitute"
    substitute_root.mkdir()
    payload = b"redirected-root-bytes"
    (substitute_root / "shard.bin").write_bytes(payload)
    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="shard-0",
        relative_path="shard.bin",
        byte_size=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        event_start_utc="2026-01-01T00:00:00Z",
        event_end_utc="2026-01-01T00:00:01Z",
    )
    attacker_called = False

    def forged_resolve_shard_root(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return substitute_root

    monkeypatch.setattr(
        shard_manifest,
        "_resolve_shard_root",
        forged_resolve_shard_root,
    )

    with pytest.raises(DatasetShardManifestError):
        shard_manifest.verify_shard_files(requested_root, [descriptor])

    assert attacker_called is False
