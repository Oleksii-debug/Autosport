from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    DatasetSnapshotUnprovenError,
    membership_manifest_sha256,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _members(*values: str) -> tuple[str, ...]:
    return tuple(_sha(value) for value in values)


def _registry(tmp_path: Path) -> ScientificRegistry:
    return ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")


def _append_snapshot(
    registry: ScientificRegistry,
    *,
    snapshot_id: str,
    members: tuple[str, ...] | None = None,
    manifest_sha256: str | None = None,
    source_identity: str = "provider:source-a",
    license_identity: str = "license:v1",
    cutoff: str = "2026-09-01T00:00:00Z",
    available_at: str = "2026-09-01T00:01:00Z",
) -> str:
    if manifest_sha256 is None:
        assert members is not None
        manifest_sha256 = membership_manifest_sha256(members)
    return registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=snapshot_id,
            manifest_sha256=manifest_sha256,
            source_identity=source_identity,
            license_identity=license_identity,
            causal_cutoff=cutoff,
            available_at_utc=available_at,
        )
    )


def _authority(tmp_path: Path, registry: ScientificRegistry) -> DatasetSnapshotLineageAuthority:
    return DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
    )


def test_true_append_is_restart_safe_and_resolves_non_parent_ancestor(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    a = _members("a", "b")
    b = _members("a", "b", "c")
    c = _members("a", "b", "c", "d")
    _append_snapshot(registry, snapshot_id="snapshot-a", members=a)
    _append_snapshot(
        registry,
        snapshot_id="snapshot-b",
        members=b,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="snapshot-c",
        members=c,
        cutoff="2026-09-03T00:00:00Z",
        available_at="2026-09-03T00:01:00Z",
    )

    authority = _authority(tmp_path, registry)
    root = authority.register(snapshot_id="snapshot-a", member_sha256=a)
    second = authority.register(
        snapshot_id="snapshot-b",
        member_sha256=b,
        parent_snapshot_id="snapshot-a",
    )
    third = authority.register(
        snapshot_id="snapshot-c",
        member_sha256=c,
        parent_snapshot_id="snapshot-b",
    )

    assert second.parent_proof_sha256 == root.proof_sha256
    assert third.parent_proof_sha256 == second.proof_sha256
    assert authority.proves_descendant(
        descendant_snapshot_id="snapshot-c", ancestor_snapshot_id="snapshot-a"
    )

    restarted = DatasetSnapshotLineageAuthority(authority.path, registry)
    resolved = restarted.require_descendant(
        descendant_snapshot_id="snapshot-c", ancestor_snapshot_id="snapshot-a"
    )
    assert resolved.proof_sha256 == third.proof_sha256
    assert restarted.register(
        snapshot_id="snapshot-c",
        member_sha256=c,
        parent_snapshot_id="snapshot-b",
    ) == third


@pytest.mark.parametrize(
    "child_members",
    [
        _members("a", "c"),
        _members("b", "a", "c"),
        _members("a", "replacement", "c"),
    ],
)
def test_rewrite_delete_or_reorder_cannot_prove_append_only_ancestry(
    tmp_path: Path,
    child_members: tuple[str, ...],
) -> None:
    registry = _registry(tmp_path)
    parent_members = _members("a", "b")
    _append_snapshot(registry, snapshot_id="parent", members=parent_members)
    _append_snapshot(
        registry,
        snapshot_id="child",
        members=child_members,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority = _authority(tmp_path, registry)
    authority.register(snapshot_id="parent", member_sha256=parent_members)

    with pytest.raises(DatasetSnapshotUnprovenError, match="preserve the parent prefix"):
        authority.register(
            snapshot_id="child",
            member_sha256=child_members,
            parent_snapshot_id="parent",
        )


def test_same_source_license_and_later_cutoff_do_not_replace_membership_proof(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    parent_members = _members("a", "b", "c")
    rewritten_members = _members("x", "y", "z")
    _append_snapshot(registry, snapshot_id="parent", members=parent_members)
    _append_snapshot(
        registry,
        snapshot_id="rewritten",
        members=rewritten_members,
        cutoff="2026-09-10T00:00:00Z",
        available_at="2026-09-10T00:01:00Z",
    )
    authority = _authority(tmp_path, registry)
    authority.register(snapshot_id="parent", member_sha256=parent_members)

    with pytest.raises(DatasetSnapshotUnprovenError):
        authority.register(
            snapshot_id="rewritten",
            member_sha256=rewritten_members,
            parent_snapshot_id="parent",
        )


def test_legacy_or_untyped_manifest_stays_unproven(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    members = _members("a", "b")
    _append_snapshot(
        registry,
        snapshot_id="legacy",
        manifest_sha256=_sha("legacy-free-form-manifest"),
    )
    authority = _authority(tmp_path, registry)

    with pytest.raises(DatasetSnapshotUnprovenError, match="canonical typed membership"):
        authority.register(snapshot_id="legacy", member_sha256=members)


def test_parent_must_be_current_tip_so_branch_substitution_fails(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root_members = _members("a")
    child_members = _members("a", "b")
    branch_members = _members("a", "c")
    _append_snapshot(registry, snapshot_id="root", members=root_members)
    _append_snapshot(
        registry,
        snapshot_id="child",
        members=child_members,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="branch",
        members=branch_members,
        cutoff="2026-09-03T00:00:00Z",
        available_at="2026-09-03T00:01:00Z",
    )
    authority = _authority(tmp_path, registry)
    authority.register(snapshot_id="root", member_sha256=root_members)
    authority.register(
        snapshot_id="child", member_sha256=child_members, parent_snapshot_id="root"
    )

    with pytest.raises(DatasetSnapshotUnprovenError, match="exact current tip"):
        authority.register(
            snapshot_id="branch",
            member_sha256=branch_members,
            parent_snapshot_id="root",
        )


def test_source_or_license_identity_cannot_cross_lineages(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    parent_members = _members("a")
    child_members = _members("a", "b")
    _append_snapshot(registry, snapshot_id="parent", members=parent_members)
    _append_snapshot(
        registry,
        snapshot_id="child",
        members=child_members,
        source_identity="provider:source-b",
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority = _authority(tmp_path, registry)
    authority.register(snapshot_id="parent", member_sha256=parent_members)

    with pytest.raises(DatasetSnapshotUnprovenError, match="source/license"):
        authority.register(
            snapshot_id="child",
            member_sha256=child_members,
            parent_snapshot_id="parent",
        )


@pytest.mark.parametrize(
    ("cutoff", "available_at", "match"),
    [
        ("2026-08-31T23:59:59Z", "2026-09-02T00:01:00Z", "cutoff moved backwards"),
        ("2026-09-02T00:00:00Z", "2026-08-31T23:59:59Z", "availability moved backwards"),
    ],
)
def test_cutoff_and_availability_must_be_monotonic(
    tmp_path: Path,
    cutoff: str,
    available_at: str,
    match: str,
) -> None:
    registry = _registry(tmp_path)
    parent_members = _members("a")
    child_members = _members("a", "b")
    _append_snapshot(registry, snapshot_id="parent", members=parent_members)
    _append_snapshot(
        registry,
        snapshot_id="child",
        members=child_members,
        cutoff=cutoff,
        available_at=available_at,
    )
    authority = _authority(tmp_path, registry)
    authority.register(snapshot_id="parent", member_sha256=parent_members)

    with pytest.raises(DatasetSnapshotUnprovenError, match=match):
        authority.register(
            snapshot_id="child",
            member_sha256=child_members,
            parent_snapshot_id="parent",
        )


def test_tampered_parent_digest_fails_closed_on_restart(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    parent_members = _members("a")
    child_members = _members("a", "b")
    _append_snapshot(registry, snapshot_id="parent", members=parent_members)
    _append_snapshot(
        registry,
        snapshot_id="child",
        members=child_members,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority = _authority(tmp_path, registry)
    authority.register(snapshot_id="parent", member_sha256=parent_members)
    authority.register(
        snapshot_id="child", member_sha256=child_members, parent_snapshot_id="parent"
    )

    state = json.loads(authority.path.read_text(encoding="utf-8"))
    state["records"][1]["parent_dataset_record_sha256"] = _sha("forged-parent")
    authority.path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="proof digest mismatch"):
        DatasetSnapshotLineageAuthority(authority.path, registry)


def test_duplicate_json_keys_and_corrupt_state_fail_closed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    path = tmp_path / "dataset-snapshot-lineage.json"
    path.write_text('{"schema_version":1,"schema_version":1,"records":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        DatasetSnapshotLineageAuthority(path, registry)

    path.write_text('{"schema_version":1,"records":', encoding="utf-8")
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        DatasetSnapshotLineageAuthority(path, registry)


def test_missing_parent_and_unproven_snapshot_fail_closed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    child_members = _members("a")
    _append_snapshot(registry, snapshot_id="child", members=child_members)
    authority = _authority(tmp_path, registry)

    assert not authority.proves_descendant(
        descendant_snapshot_id="child", ancestor_snapshot_id="missing"
    )
    with pytest.raises(DatasetSnapshotUnprovenError, match="parent DatasetSnapshot"):
        authority.register(
            snapshot_id="child",
            member_sha256=child_members,
            parent_snapshot_id="missing",
        )
