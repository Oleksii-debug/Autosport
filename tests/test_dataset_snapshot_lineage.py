from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autosport.dataset_snapshot_lineage import (
    ConflictingDatasetSnapshotLineageError,
    DatasetSnapshotLineageAuthority,
    DatasetSnapshotLineageError,
    membership_sha256,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _append_snapshot(
    registry: ScientificRegistry,
    *,
    snapshot_id: str,
    members: tuple[str, ...],
    cutoff: str,
    available_at: str,
    source: str = "provider:paper",
    license_identity: str = "license:v1",
    manifest_sha256: str | None = None,
) -> None:
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=snapshot_id,
            manifest_sha256=manifest_sha256 or membership_sha256(members),
            source_identity=source,
            license_identity=license_identity,
            causal_cutoff=cutoff,
            available_at_utc=available_at,
        )
    )


def _authority(tmp_path: Path) -> tuple[ScientificRegistry, DatasetSnapshotLineageAuthority]:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    authority = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry=registry,
    )
    return registry, authority


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_multi_generation_append_only_ancestry_survives_restart(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    first = (_sha("row-1"), _sha("row-2"))
    second = (*first, _sha("row-3"))
    third = (*second, _sha("row-4"))
    _append_snapshot(
        registry,
        snapshot_id="dataset-root",
        members=first,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="dataset-child",
        members=second,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="dataset-grandchild",
        members=third,
        cutoff="2026-09-03T00:00:00Z",
        available_at="2026-09-03T00:01:00Z",
    )

    authority.register(snapshot_id="dataset-root", member_sha256s=first)
    authority.register(
        snapshot_id="dataset-child",
        member_sha256s=second,
        parent_snapshot_id="dataset-root",
    )
    authority.register(
        snapshot_id="dataset-grandchild",
        member_sha256s=third,
        parent_snapshot_id="dataset-child",
    )

    restarted = DatasetSnapshotLineageAuthority(
        tmp_path / "dataset-snapshot-lineage.json",
        registry=registry,
    )
    proof = restarted.prove_descendant(
        ancestor_snapshot_id="dataset-root",
        descendant_snapshot_id="dataset-grandchild",
    )

    assert proof.chain == ("dataset-root", "dataset-child", "dataset-grandchild")
    assert proof.ancestor_membership_sha256 == membership_sha256(first)
    assert proof.descendant_membership_sha256 == membership_sha256(third)


def test_unrelated_rewrite_with_same_source_license_and_later_cutoff_fails(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    first = (_sha("row-1"), _sha("row-2"))
    rewritten = (_sha("row-1"), _sha("rewritten-row-2"), _sha("row-3"))
    _append_snapshot(
        registry,
        snapshot_id="dataset-root",
        members=first,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="dataset-rewrite",
        members=rewritten,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="dataset-root", member_sha256s=first)

    with pytest.raises(
        DatasetSnapshotLineageError,
        match="does not preserve complete parent membership prefix",
    ):
        authority.register(
            snapshot_id="dataset-rewrite",
            member_sha256s=rewritten,
            parent_snapshot_id="dataset-root",
        )


@pytest.mark.parametrize(
    "child_members",
    [
        (_sha("row-1"), _sha("row-3")),
        (_sha("row-2"), _sha("row-1"), _sha("row-3")),
        (_sha("row-1"), _sha("replacement"), _sha("row-3")),
    ],
)
def test_delete_reorder_or_replace_parent_member_fails(
    tmp_path: Path,
    child_members: tuple[str, ...],
) -> None:
    registry, authority = _authority(tmp_path)
    first = (_sha("row-1"), _sha("row-2"))
    _append_snapshot(
        registry,
        snapshot_id="dataset-root",
        members=first,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="dataset-child",
        members=child_members,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="dataset-root", member_sha256s=first)

    with pytest.raises(DatasetSnapshotLineageError):
        authority.register(
            snapshot_id="dataset-child",
            member_sha256s=child_members,
            parent_snapshot_id="dataset-root",
        )


def test_caller_members_are_rejected_when_registry_manifest_commits_elsewhere(
    tmp_path: Path,
) -> None:
    registry, authority = _authority(tmp_path)
    real_members = (_sha("real-row"),)
    claimed_members = (_sha("invented-row"),)
    _append_snapshot(
        registry,
        snapshot_id="snapshot",
        members=real_members,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )

    with pytest.raises(
        DatasetSnapshotLineageError,
        match="manifest does not commit to exact lineage membership",
    ):
        authority.register(
            snapshot_id="snapshot",
            member_sha256s=claimed_members,
        )


def test_legacy_arbitrary_manifest_is_unproven_even_with_claimed_members(
    tmp_path: Path,
) -> None:
    registry, authority = _authority(tmp_path)
    members = (_sha("row-1"),)
    _append_snapshot(
        registry,
        snapshot_id="legacy",
        members=members,
        manifest_sha256=_sha("legacy-unstructured-manifest"),
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )

    with pytest.raises(
        DatasetSnapshotLineageError,
        match="manifest does not commit to exact lineage membership",
    ):
        authority.register(snapshot_id="legacy", member_sha256s=members)


def test_source_license_and_cutoff_changes_fail_closed(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    root_members = (_sha("row-1"),)
    child_members = (*root_members, _sha("row-2"))
    _append_snapshot(
        registry,
        snapshot_id="root",
        members=root_members,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="root", member_sha256s=root_members)

    cases = (
        ("wrong-source", "2026-09-03T00:00:00Z", "provider:other", "license:v1"),
        ("wrong-license", "2026-09-03T00:00:00Z", "provider:paper", "license:v2"),
        ("backwards-cutoff", "2026-09-01T00:00:00Z", "provider:paper", "license:v1"),
    )
    for snapshot_id, cutoff, source, license_identity in cases:
        _append_snapshot(
            registry,
            snapshot_id=snapshot_id,
            members=child_members,
            cutoff=cutoff,
            available_at="2026-09-03T00:01:00Z",
            source=source,
            license_identity=license_identity,
        )
        with pytest.raises(DatasetSnapshotLineageError):
            authority.register(
                snapshot_id=snapshot_id,
                member_sha256s=child_members,
                parent_snapshot_id="root",
            )


def test_exact_retry_is_idempotent_and_conflicting_retry_is_rejected(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    members = (_sha("row-1"), _sha("row-2"))
    _append_snapshot(
        registry,
        snapshot_id="root",
        members=members,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )

    first = authority.register(snapshot_id="root", member_sha256s=members)
    second = authority.register(snapshot_id="root", member_sha256s=members)
    assert first == second

    with pytest.raises(DatasetSnapshotLineageError):
        authority.register(
            snapshot_id="root",
            member_sha256s=(*members, _sha("row-3")),
        )


def test_conflicting_parent_retry_is_rejected(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    root_members = (_sha("row-1"),)
    child_members = (*root_members, _sha("row-2"))
    _append_snapshot(
        registry,
        snapshot_id="root",
        members=root_members,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="child",
        members=child_members,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="root", member_sha256s=root_members)
    authority.register(
        snapshot_id="child",
        member_sha256s=child_members,
        parent_snapshot_id="root",
    )

    with pytest.raises(ConflictingDatasetSnapshotLineageError):
        authority.register(snapshot_id="child", member_sha256s=child_members)


def test_tampered_state_digest_fails_before_ancestry_resolution(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    members = (_sha("row-1"),)
    _append_snapshot(
        registry,
        snapshot_id="root",
        members=members,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    authority.register(snapshot_id="root", member_sha256s=members)

    path = tmp_path / "dataset-snapshot-lineage.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["records"][0]["member_sha256s"][0] = _sha("tampered-row")
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="state digest mismatch"):
        DatasetSnapshotLineageAuthority(path, registry=registry)


def test_recomputed_tamper_cannot_substitute_parent_registry_record(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    root_members = (_sha("row-1"),)
    child_members = (*root_members, _sha("row-2"))
    _append_snapshot(
        registry,
        snapshot_id="root",
        members=root_members,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="child",
        members=child_members,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="root", member_sha256s=root_members)
    authority.register(
        snapshot_id="child",
        member_sha256s=child_members,
        parent_snapshot_id="root",
    )

    path = tmp_path / "dataset-snapshot-lineage.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    child = state["records"][1]
    child["parent_snapshot_record_sha256"] = _sha("forged-parent-record")
    child_without_digest = dict(child)
    child_without_digest.pop("record_sha256")
    child["record_sha256"] = _canonical_digest(child_without_digest)
    state["state_sha256"] = _canonical_digest(
        {"schema_version": 1, "records": state["records"]}
    )
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(DatasetSnapshotLineageError, match="parent record digest mismatch"):
        DatasetSnapshotLineageAuthority(path, registry=registry)


def test_unregistered_snapshot_remains_unproven(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    first = (_sha("row-1"),)
    second = (*first, _sha("row-2"))
    _append_snapshot(
        registry,
        snapshot_id="legacy",
        members=first,
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="later",
        members=second,
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="later", member_sha256s=second)

    with pytest.raises(
        DatasetSnapshotLineageError,
        match="both snapshots require durable lineage authority",
    ):
        authority.prove_descendant(
            ancestor_snapshot_id="legacy",
            descendant_snapshot_id="later",
        )
