from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.dataset_shard_manifest as shard_manifest
from autosport.dataset_shard_manifest import (
    DatasetShardDescriptor,
    DatasetShardManifestError,
    verify_registered_dataset_shards,
    verify_shard_files,
)
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import ScientificRegistry


def test_public_verifier_ignores_rebound_registered_manifest_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"helper-dispatch-falsifier"
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
    forged = SimpleNamespace(
        member_sha256=manifest.member_sha256,
        manifest_sha256=manifest.membership_manifest_sha256(),
        causal_cutoff="2026-09-01T00:59:59Z",
    )

    monkeypatch.setattr(
        shard_manifest,
        "_require_registered_manifest",
        lambda *_args, **_kwargs: forged,
    )

    with pytest.raises(
        DatasetShardManifestError,
        match="canonical|lineage|proof|authority",
    ):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )


@pytest.mark.parametrize("attr_name", ["S_ISLNK", "S_ISDIR"])
def test_public_verifier_rejects_root_stat_dispatch_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attr_name: str,
) -> None:
    payload = b"root-stat-dispatch-falsifier"
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
    hostile_calls: list[str] = []

    def forged_mode_check(_mode: int) -> bool:
        hostile_calls.append(attr_name)
        return attr_name == "S_ISDIR"

    monkeypatch.setattr(shard_manifest.stat, attr_name, forged_mode_check)

    with pytest.raises(
        DatasetShardManifestError,
        match="canonical|dependency|dispatch|authority",
    ):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )

    assert hostile_calls == []


@pytest.mark.parametrize("attr_name", ["__eq__", "__ne__", "parent", "name"])
def test_public_verifier_rejects_path_identity_dispatch_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attr_name: str,
) -> None:
    payload = b"path-identity-dispatch-falsifier"
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
    hostile_calls: list[str] = []
    concrete_path_type = type(shard_manifest.Path())

    if attr_name in {"__eq__", "__ne__"}:
        def forged_compare(_self: Path, _other: object) -> bool:
            hostile_calls.append(attr_name)
            return attr_name == "__eq__"

        replacement = forged_compare
    else:
        def forged_property(_self: Path) -> object:
            hostile_calls.append(attr_name)
            return tmp_path

        replacement = property(forged_property)

    monkeypatch.setattr(concrete_path_type, attr_name, replacement)

    with pytest.raises(
        DatasetShardManifestError,
        match="canonical|dependency|dispatch|authority",
    ):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )

    assert hostile_calls == []


@pytest.mark.parametrize(
    ("owner_label", "attr_name"),
    [
        ("os", "fspath"),
        ("os", "getcwd"),
        ("os", "getcwdb"),
        ("os", "fsdecode"),
        ("os", "fsencode"),
        ("os", "stat"),
        ("os", "lstat"),
        ("os", "readlink"),
        ("os.path", "realpath"),
        ("os.path", "expanduser"),
    ],
)
def test_public_verifier_rejects_pathlib_os_dispatch_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_label: str,
    attr_name: str,
) -> None:
    payload = b"pathlib-os-dispatch-falsifier"
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
    hostile_calls: list[str] = []
    owner = shard_manifest.os if owner_label == "os" else shard_manifest.os.path

    def hostile(*_args, **_kwargs):
        hostile_calls.append(f"{owner_label}.{attr_name}")
        raise AssertionError("hostile pathlib OS dispatch must not execute")

    monkeypatch.setattr(owner, attr_name, hostile)

    with pytest.raises(
        DatasetShardManifestError,
        match="canonical|dependency|dispatch|authority",
    ):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )

    assert hostile_calls == []


@pytest.mark.parametrize(
    "target",
    ["realpath.direct-global", "path.is_symlink.direct-global"],
)
def test_public_verifier_rejects_external_dependency_global_rebind(
    tmp_path: Path,
    target: str,
) -> None:
    payload = b"external-global-dispatch-falsifier"
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
    hostile_calls: list[str] = []

    if target == "realpath.direct-global":
        executable = shard_manifest.os.path.realpath
    else:
        concrete_path_type = type(shard_manifest.Path())
        executable = concrete_path_type.is_symlink

    globals_dict = executable.__globals__
    direct_globals = [
        name
        for name in executable.__code__.co_names
        if name in globals_dict and callable(globals_dict[name])
    ]
    if not direct_globals:
        pytest.skip("platform implementation exposes no callable direct global")
    global_name = direct_globals[0]
    canonical = globals_dict[global_name]

    def hostile(*args, **kwargs):
        hostile_calls.append(target)
        return canonical(*args, **kwargs)

    globals_dict[global_name] = hostile
    try:
        with pytest.raises(
            DatasetShardManifestError,
            match="canonical|dependency|globals|dispatch|authority",
        ):
            verify_registered_dataset_shards(
                authority,
                snapshot_id="snapshot-1",
                shard_root=tmp_path,
                shards=(descriptor,),
            )
    finally:
        globals_dict[global_name] = canonical

    assert hostile_calls == []


def test_public_verifier_rejects_os_environ_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"os-environ-dispatch-falsifier"
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

    monkeypatch.setattr(shard_manifest.os, "environ", dict(shard_manifest.os.environ))

    with pytest.raises(
        DatasetShardManifestError,
        match="canonical|dependency|dispatch|authority",
    ):
        verify_registered_dataset_shards(
            authority,
            snapshot_id="snapshot-1",
            shard_root=tmp_path,
            shards=(descriptor,),
        )

