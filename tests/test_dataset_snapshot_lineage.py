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
    manifest: str,
    cutoff: str,
    available_at: str,
    source: str = "provider:paper",
    license_identity: str = "license:v1",
) -> None:
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=snapshot_id,
            manifest_sha256=_sha(manifest),
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
    _append_snapshot(
        registry,
        snapshot_id="dataset-root",
        manifest="manifest-root",
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="dataset-child",
        manifest="manifest-child",
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="dataset-grandchild",
        manifest="manifest-grandchild",
        cutoff="2026-09-03T00:00:00Z",
        available_at="2026-09-03T00:01:00Z",
    )

    first = (_sha("row-1"), _sha("row-2"))
    second = (*first, _sha("row-3"))
    third = (*second, _sha("row-4"))
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
    _append_snapshot(
        registry,
        snapshot_id="dataset-root",
        manifest="manifest-root",
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="dataset-rewrite",
        manifest="manifest-rewrite",
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(
        snapshot_id="dataset-root",
        member_sha256s=(_sha("row-1"), _sha("row-2")),
    )

    with pytest.raises(
        DatasetSnapshotLineageError,
        match="does not preserve complete parent membership prefix",
    ):
        authority.register(
            snapshot_id="dataset-rewrite",
            member_sha256s=(_sha("row-1"), _sha("rewritten-row-2"), _sha("row-3")),
            parent_snapshot_id="dataset-root",
        )


@pytest.mark.parametrize(
    "members",
    [
        lambda first: (first[0], _sha("row-3")),
        lambda first: (first[1], first[0], _sha("row-3")),
        lambda first: (first[0], _sha("replacement"), _sha("row-3")),
    ],
)
def test_delete_reorder_or_replace_parent_member_fails(
    tmp_path: Path,
    members,
) -> None:
    registry, authority = _authority(tmp_path)
    _append_snapshot(
        registry,
        snapshot_id="dataset-root",
        manifest="manifest-root",
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="dataset-child",
        manifest="manifest-child",
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    first = (_sha("row-1"), _sha("row-2"))
    authority.register(snapshot_id="dataset-root", member_sha256s=first)

    with pytest.raises(DatasetSnapshotLineageError):
        authority.register(
            snapshot_id="dataset-child",
            member_sha256s=members(first),
            parent_snapshot_id="dataset-root",
        )


def test_source_license_cutoff_and_manifest_semantics_fail_closed(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    _append_snapshot(
        registry,
        snapshot_id="root",
        manifest="manifest-root",
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="root", member_sha256s=(_sha("row-1"),))

    cases = (
        ("wrong-source", "manifest-source", "2026-09-03T00:00:00Z", "provider:other", "license:v1", (_sha("row-1"), _sha("row-2"))),
        ("wrong-license", "manifest-license", "2026-09-03T00:00:00Z", "provider:paper", "license:v2", (_sha("row-1"), _sha("row-2"))),
        ("backwards-cutoff", "manifest-cutoff", "2026-09-01T00:00:00Z", "provider:paper", "license:v1", (_sha("row-1"), _sha("row-2"))),
        ("changed-manifest-no-append", "manifest-no-append", "2026-09-03T00:00:00Z", "provider:paper", "license:v1", (_sha("row-1"),)),
    )
    for snapshot_id, manifest, cutoff, source, license_identity, child_members in cases:
        _append_snapshot(
            registry,
            snapshot_id=snapshot_id,
            manifest=manifest,
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
    _append_snapshot(
        registry,
        snapshot_id="root",
        manifest="manifest-root",
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    members = (_sha("row-1"), _sha("row-2"))

    first = authority.register(snapshot_id="root", member_sha256s=members)
    second = authority.register(snapshot_id="root", member_sha256s=members)
    assert first == second

    with pytest.raises(ConflictingDatasetSnapshotLineageError):
        authority.register(
            snapshot_id="root",
            member_sha256s=(*members, _sha("row-3")),
        )


def test_tampered_state_digest_fails_before_ancestry_resolution(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    _append_snapshot(
        registry,
        snapshot_id="root",
        manifest="manifest-root",
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    authority.register(snapshot_id="root", member_sha256s=(_sha("row-1"),))

    path = tmp_path / "dataset-snapshot-lineage.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["records"][0]["member_sha256s"][0] = _sha("tampered-row")
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="state digest mismatch"):
        DatasetSnapshotLineageAuthority(path, registry=registry)


def test_recomputed_tamper_cannot_substitute_parent_registry_record(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    _append_snapshot(
        registry,
        snapshot_id="root",
        manifest="manifest-root",
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="child",
        manifest="manifest-child",
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="root", member_sha256s=(_sha("row-1"),))
    authority.register(
        snapshot_id="child",
        member_sha256s=(_sha("row-1"), _sha("row-2")),
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


def test_unregistered_legacy_snapshot_remains_unproven(tmp_path: Path) -> None:
    registry, authority = _authority(tmp_path)
    _append_snapshot(
        registry,
        snapshot_id="legacy",
        manifest="manifest-legacy",
        cutoff="2026-09-01T00:00:00Z",
        available_at="2026-09-01T00:01:00Z",
    )
    _append_snapshot(
        registry,
        snapshot_id="later",
        manifest="manifest-later",
        cutoff="2026-09-02T00:00:00Z",
        available_at="2026-09-02T00:01:00Z",
    )
    authority.register(snapshot_id="later", member_sha256s=(_sha("row-1"),))

    with pytest.raises(
        DatasetSnapshotLineageError,
        match="both snapshots require durable lineage authority",
    ):
        authority.prove_descendant(
            ancestor_snapshot_id="legacy",
            descendant_snapshot_id="later",
        )
