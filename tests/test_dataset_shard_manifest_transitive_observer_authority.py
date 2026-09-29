from __future__ import annotations

import hashlib

import pytest

import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
)


def test_verify_shard_files_rejects_transitive_observer_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    """Sealing one helper hop must not leave its byte observer caller-controlled."""

    payload = b"canonical-shard-bytes"
    path = tmp_path / "shard.bin"
    path.write_bytes(payload)
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

    def forged_observe_stable_regular_file(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return len(payload), hashlib.sha256(payload).hexdigest(), None

    monkeypatch.setattr(
        shard_manifest,
        "_observe_stable_regular_file",
        forged_observe_stable_regular_file,
    )

    with pytest.raises(DatasetShardManifestError):
        shard_manifest.verify_shard_files(tmp_path, [descriptor])

    assert attacker_called is False
