from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import pytest

# Focused local harness: production CI imports the real canonical lineage module.
# This stub reproduces only the public membership-manifest function needed by the
# stateless shard verifier, so the new source can be adversarially tested offline.
stub = types.ModuleType("autosport.dataset_snapshot_lineage")

def _membership_manifest_sha256(members: tuple[str, ...]) -> str:
    import json
    payload = {
        "kind": "autosport-dataset-membership-manifest-v1",
        "schema_version": 1,
        "member_sha256": list(members),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()

stub.membership_manifest_sha256 = _membership_manifest_sha256
sys.modules.setdefault("autosport.dataset_snapshot_lineage", stub)

from autosport.dataset_shard_manifest import (  # noqa: E402
    DatasetShardDescriptor,
    DatasetShardManifestError,
    canonical_shard_manifest,
    read_registered_shard_bytes,
    verify_registered_dataset_shards,
    verify_shard_files,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _descriptor(
    ordinal: int,
    shard_id: str,
    path: str,
    payload: bytes,
    *,
    start: str = "2026-09-01T00:00:00Z",
    end: str = "2026-09-01T00:59:59Z",
) -> DatasetShardDescriptor:
    return DatasetShardDescriptor(
        ordinal=ordinal,
        shard_id=shard_id,
        relative_path=path,
        byte_size=len(payload),
        content_sha256=_sha(payload),
        event_start_utc=start,
        event_end_utc=end,
    )


class _Record:
    def __init__(self, members: tuple[str, ...], manifest: str) -> None:
        self.member_sha256 = members
        self.manifest_sha256 = manifest


class _Authority:
    def __init__(self, record: _Record | None) -> None:
        self._record = record

    def record(self, snapshot_id: str) -> _Record | None:
        assert snapshot_id == "snapshot-1"
        return self._record


def test_order_is_canonical_and_descriptor_metadata_is_committed(tmp_path: Path) -> None:
    a = b"alpha"
    b = b"beta"
    (tmp_path / "a.bin").write_bytes(a)
    (tmp_path / "b.bin").write_bytes(b)
    first = _descriptor(0, "a", "a.bin", a)
    second = _descriptor(1, "b", "b.bin", b)

    forward = verify_shard_files(tmp_path, (first, second))
    reverse = verify_shard_files(tmp_path, (second, first))

    assert forward == reverse
    assert forward.member_sha256 == reverse.member_sha256
    changed_time = _descriptor(
        0,
        "a",
        "a.bin",
        a,
        start="2026-09-01T00:00:01Z",
    )
    assert changed_time.member_sha256 != first.member_sha256


def test_byte_tamper_and_size_tamper_fail_closed(tmp_path: Path) -> None:
    payload = b"payload"
    path = tmp_path / "shard.bin"
    path.write_bytes(payload)
    descriptor = _descriptor(0, "s0", "shard.bin", payload)
    verify_shard_files(tmp_path, (descriptor,))

    path.write_bytes(b"tampered")
    with pytest.raises(DatasetShardManifestError, match="byte-size|SHA-256"):
        verify_shard_files(tmp_path, (descriptor,))

    path.write_bytes(payload)
    wrong_size = DatasetShardDescriptor(
        ordinal=0,
        shard_id="s0",
        relative_path="shard.bin",
        byte_size=len(payload) + 1,
        content_sha256=_sha(payload),
        event_start_utc="2026-09-01T00:00:00Z",
        event_end_utc="2026-09-01T00:59:59Z",
    )
    with pytest.raises(DatasetShardManifestError, match="byte-size"):
        verify_shard_files(tmp_path, (wrong_size,))


@pytest.mark.parametrize(
    "relative_path",
    ["../escape.bin", "/absolute.bin", "a\\b.bin", "./x.bin", "a//b.bin", "C:/x.bin", "nested/a:b.bin", "CON", "con.txt", "nested/LPT9.csv", "name.", "name ", "bad\x01name.bin", "bad?.bin", "bad*.bin", "bad|name.bin", "bad<name>.bin", 'bad"name.bin'],
)
def test_nonportable_or_escaping_paths_are_rejected(relative_path: str) -> None:
    with pytest.raises(ValueError, match="relative_path"):
        _descriptor(0, "s0", relative_path, b"x")


def test_symlink_shard_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"x")
    link = tmp_path / "link.bin"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable in this environment")
    descriptor = _descriptor(0, "s0", "link.bin", b"x")
    with pytest.raises(DatasetShardManifestError, match="symlink"):
        verify_shard_files(tmp_path, (descriptor,))


def test_duplicate_ids_paths_and_noncontiguous_ordinals_are_rejected() -> None:
    a = _descriptor(0, "same", "a.bin", b"a")
    dup_id = _descriptor(1, "same", "b.bin", b"b")
    with pytest.raises(ValueError, match="shard_id"):
        canonical_shard_manifest((a, dup_id))

    dup_path = _descriptor(1, "other", "a.bin", b"b")
    with pytest.raises(ValueError, match="relative_path"):
        canonical_shard_manifest((a, dup_path))

    case_collision = _descriptor(1, "other", "A.BIN", b"b")
    with pytest.raises(ValueError, match="portable filesystems"):
        canonical_shard_manifest((a, case_collision))

    gap = _descriptor(2, "other", "b.bin", b"b")
    with pytest.raises(ValueError, match="contiguous"):
        canonical_shard_manifest((a, gap))


def test_explicit_utc_and_event_range_are_canonicalized_and_checked() -> None:
    descriptor = _descriptor(
        0,
        "s0",
        "a.bin",
        b"x",
        start="2026-09-01T00:00:00.1Z",
        end="2026-09-01T00:00:00.200000Z",
    )
    assert descriptor.event_start_utc == "2026-09-01T00:00:00.100000Z"
    assert descriptor.event_end_utc == "2026-09-01T00:00:00.200000Z"

    with pytest.raises(ValueError, match="explicit UTC"):
        _descriptor(0, "s0", "a.bin", b"x", start="2026-09-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="must not precede"):
        _descriptor(
            0,
            "s0",
            "a.bin",
            b"x",
            start="2026-09-01T00:00:01Z",
            end="2026-09-01T00:00:00Z",
        )


def test_canonical_lineage_binding_rejects_changed_descriptor_or_bytes(tmp_path: Path) -> None:
    payload = b"dataset"
    (tmp_path / "s.bin").write_bytes(payload)
    descriptor = _descriptor(0, "s0", "s.bin", payload)
    manifest = verify_shard_files(tmp_path, (descriptor,))
    record = _Record(manifest.member_sha256, manifest.membership_manifest_sha256())
    authority = _Authority(record)

    assert verify_registered_dataset_shards(
        authority,
        snapshot_id="snapshot-1",
        shard_root=tmp_path,
        shards=(descriptor,),
    ) is record
    assert read_registered_shard_bytes(
        authority,
        snapshot_id="snapshot-1",
        shard_root=tmp_path,
        shards=(descriptor,),
        shard_id="s0",
    ) == payload

    changed_metadata = _descriptor(
        0,
        "s0",
        "s.bin",
        payload,
        start="2026-09-01T00:00:01Z",
    )
    with pytest.raises(DatasetShardManifestError, match="commitments"):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(changed_metadata,),
        )

    (tmp_path / "s.bin").write_bytes(b"rewritten")
    with pytest.raises(DatasetShardManifestError, match="byte-size|SHA-256"):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )


def test_missing_canonical_lineage_record_fails_closed(tmp_path: Path) -> None:
    payload = b"x"
    (tmp_path / "s.bin").write_bytes(payload)
    descriptor = _descriptor(0, "s0", "s.bin", payload)
    with pytest.raises(DatasetShardManifestError, match="no canonical lineage"):
        verify_registered_dataset_shards(
            _Authority(None),
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )


def test_durable_payload_roundtrip_is_strict_and_order_independent() -> None:
    a = _descriptor(0, "a", "a.bin", b"a")
    b = _descriptor(1, "b", "b.bin", b"b")
    manifest = canonical_shard_manifest((b, a))
    restored = type(manifest).from_payload(manifest.payload())
    assert restored == manifest
    assert restored.descriptor_set_sha256 == manifest.descriptor_set_sha256

    bad = manifest.payload()
    bad["unknown"] = True
    with pytest.raises(ValueError, match="fields mismatch"):
        type(manifest).from_payload(bad)

    shard_bad = a.payload()
    shard_bad["unknown"] = True
    with pytest.raises(ValueError, match="fields mismatch"):
        DatasetShardDescriptor.from_payload(shard_bad)

    schema_bad = a.payload()
    schema_bad["schema_version"] = 2
    with pytest.raises(ValueError, match="schema mismatch"):
        DatasetShardDescriptor.from_payload(schema_bad)


def test_read_revalidates_target_after_authority_lookup_to_close_toctou(
    tmp_path: Path,
) -> None:
    payload = b"AAAA"
    rewritten = b"BBBB"
    path = tmp_path / "s.bin"
    path.write_bytes(payload)
    descriptor = _descriptor(0, "s0", "s.bin", payload)
    manifest = verify_shard_files(tmp_path, (descriptor,))
    record = _Record(manifest.member_sha256, manifest.membership_manifest_sha256())

    class _MutatingAuthority(_Authority):
        def record(self, snapshot_id: str) -> _Record | None:
            result = super().record(snapshot_id)
            path.write_bytes(rewritten)
            return result

    with pytest.raises(DatasetShardManifestError, match="SHA-256"):
        read_registered_shard_bytes(
            _MutatingAuthority(record),
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
            shard_id="s0",
        )
