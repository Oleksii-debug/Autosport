from __future__ import annotations

import pytest

import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
)


def test_verify_shard_files_rejects_transitive_descriptor_verifier_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    """Captured parent code identity must not hide mutable child dispatch.

    `verify_shard_files` is reused by the sealed registered-shard entrypoints. If its
    module-global `_verify_descriptor_bytes` lookup can be rebound independently, a
    caller can make mismatching or missing bytes appear verified while the captured
    parent function object and `__code__` remain unchanged.
    """

    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="shard-0",
        relative_path="missing-shard.bin",
        byte_size=1,
        content_sha256="0" * 64,
        event_start_utc="2026-01-01T00:00:00Z",
        event_end_utc="2026-01-01T00:00:01Z",
    )
    attacker_called = False

    def forged_verify_descriptor_bytes(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return None

    monkeypatch.setattr(
        shard_manifest,
        "_verify_descriptor_bytes",
        forged_verify_descriptor_bytes,
    )

    with pytest.raises(DatasetShardManifestError):
        shard_manifest.verify_shard_files(tmp_path, [descriptor])

    assert attacker_called is False
