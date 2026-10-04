from __future__ import annotations

import hashlib

import pytest

import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
)


def test_verify_shard_files_rejects_transitive_candidate_path_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    """Root/path confinement cannot depend on a mutable late-resolved helper."""

    substitute_payload = b"attacker-selected-shard"
    substitute = tmp_path / "substitute.bin"
    substitute.write_bytes(substitute_payload)
    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="shard-0",
        relative_path="missing.bin",
        byte_size=len(substitute_payload),
        content_sha256=hashlib.sha256(substitute_payload).hexdigest(),
        event_start_utc="2026-01-01T00:00:00Z",
        event_end_utc="2026-01-01T00:00:01Z",
    )
    attacker_called = False

    def forged_candidate_path(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return substitute

    monkeypatch.setattr(
        shard_manifest,
        "_candidate_path",
        forged_candidate_path,
    )

    with pytest.raises(DatasetShardManifestError):
        shard_manifest.verify_shard_files(tmp_path, [descriptor])

    assert attacker_called is False
