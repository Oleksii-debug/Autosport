from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import autosport._dataset_shard_manifest_authority_guard  # noqa: F401
import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifest,
    DatasetShardManifestError,
)


def test_registered_shard_authority_rejects_in_place_kwdefault_mint(
    tmp_path,
) -> None:
    """Mutable function kwdefaults cannot replace the canonical lineage authority."""

    manifest = DatasetShardManifest(())
    forged_record = SimpleNamespace(
        member_sha256=manifest.member_sha256,
        manifest_sha256=manifest.membership_manifest_sha256(),
        causal_cutoff="2026-09-01T00:00:00Z",
    )

    class ForgedAuthority:
        def record(self, snapshot_id: str):
            assert snapshot_id == "snapshot-1"
            return forged_record

    canonical = shard_manifest._require_registered_manifest
    kwdefaults = canonical.__kwdefaults__
    assert kwdefaults is not None
    original = dict(kwdefaults)
    try:
        kwdefaults["_authority_type"] = ForgedAuthority
        kwdefaults["_record_reader"] = ForgedAuthority.record
        kwdefaults["_record_reader_code"] = ForgedAuthority.record.__code__

        with pytest.raises(
            DatasetShardManifestError,
            match="registered shard authority defaults changed",
        ):
            shard_manifest.verify_registered_dataset_shards(
                ForgedAuthority(),
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(),
            )
    finally:
        kwdefaults.clear()
        kwdefaults.update(original)


def test_registered_shard_authority_rejects_module_builtin_shadow(
    tmp_path,
) -> None:
    """A module-global builtin shadow cannot forge the internal executable witnesses."""

    had_global = "getattr" in shard_manifest.__dict__
    original = shard_manifest.__dict__.get("getattr")
    shard_manifest.getattr = lambda obj, name, default=None: default
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="dataset shard builtin dispatch changed: getattr",
        ):
            shard_manifest.verify_registered_dataset_shards(
                object(),
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(),
            )
    finally:
        if had_global:
            shard_manifest.getattr = original
        else:
            shard_manifest.__dict__.pop("getattr", None)


def test_registered_shard_entry_detaches_hostile_iterable_before_authority_preflight(
    tmp_path,
) -> None:
    """Caller iteration cannot mutate shard verification inside the trusted interval."""

    expected = b"expected-dataset-bytes"
    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="s0",
        relative_path="missing.bin",
        byte_size=len(expected),
        content_sha256=hashlib.sha256(expected).hexdigest(),
        event_start_utc="2026-09-01T00:00:00Z",
        event_end_utc="2026-09-01T00:59:59Z",
    )
    stale_namespace = dict(shard_manifest.__dict__)
    original_verify = shard_manifest._verify_descriptor_bytes
    had_globals = "globals" in shard_manifest.__dict__
    original_globals = shard_manifest.__dict__.get("globals")
    hostile_calls = 0

    def restore() -> None:
        shard_manifest._verify_descriptor_bytes = original_verify
        if had_globals:
            shard_manifest.globals = original_globals
        else:
            shard_manifest.__dict__.pop("globals", None)

    def forged_verify(*_args, **_kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        # The historical bypass could self-restore before the outer post-check,
        # turning the final positive result into an apparently canonical call.
        restore()
        return None

    class HostileShards:
        def __iter__(self):
            # Make the old inner helper guard observe a stale canonical namespace while
            # real module dispatch points at the forged byte verifier.
            shard_manifest.globals = lambda: stale_namespace
            shard_manifest._verify_descriptor_bytes = forged_verify
            yield descriptor

    try:
        with pytest.raises(
            DatasetShardManifestError,
            match=(
                "dataset shard (?:builtin dispatch changed: globals|"
                "module authority changed: _verify_descriptor_bytes)"
            ),
        ):
            shard_manifest.verify_registered_dataset_shards(
                object(),
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=HostileShards(),
            )
        assert hostile_calls == 0
    finally:
        restore()


def test_registered_shard_entry_detaches_hostile_pathlike_before_authority_preflight(
    tmp_path,
) -> None:
    """Caller ``__fspath__`` cannot mutate shard verification inside the trusted interval."""

    expected = b"expected-dataset-bytes"
    descriptor = DatasetShardDescriptor(
        ordinal=0,
        shard_id="s0",
        relative_path="missing.bin",
        byte_size=len(expected),
        content_sha256=hashlib.sha256(expected).hexdigest(),
        event_start_utc="2026-09-01T00:00:00Z",
        event_end_utc="2026-09-01T00:59:59Z",
    )
    stale_namespace = dict(shard_manifest.__dict__)
    original_verify = shard_manifest._verify_descriptor_bytes
    had_globals = "globals" in shard_manifest.__dict__
    original_globals = shard_manifest.__dict__.get("globals")
    hostile_calls = 0

    def restore() -> None:
        shard_manifest._verify_descriptor_bytes = original_verify
        if had_globals:
            shard_manifest.globals = original_globals
        else:
            shard_manifest.__dict__.pop("globals", None)

    def forged_verify(*_args, **_kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        restore()
        return None

    class HostileRoot:
        def __fspath__(self):
            shard_manifest.globals = lambda: stale_namespace
            shard_manifest._verify_descriptor_bytes = forged_verify
            return str(tmp_path)

    try:
        with pytest.raises(
            DatasetShardManifestError,
            match=(
                "dataset shard (?:builtin dispatch changed: globals|"
                "module authority changed: _verify_descriptor_bytes)"
            ),
        ):
            shard_manifest.verify_registered_dataset_shards(
                object(),
                snapshot_id="snapshot-1",
                shard_root=HostileRoot(),
                shards=(descriptor,),
            )
        assert hostile_calls == 0
    finally:
        restore()
