from __future__ import annotations

import hashlib

import pytest

from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.policy_deployment import (
    PolicyDeploymentError,
    _require_dataset_lineage,
    dataset_lineage_authority_path,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _members(*values: str) -> tuple[str, ...]:
    return tuple(_sha(value) for value in values)


def _append_snapshot(
    registry: ScientificRegistry,
    *,
    snapshot_id: str,
    members: tuple[str, ...],
    cutoff: str,
    available_at: str,
) -> str:
    return registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=snapshot_id,
            manifest_sha256=membership_manifest_sha256(members),
            source_identity="lawful-provider:paper",
            license_identity="paper-license-v1",
            causal_cutoff=cutoff,
            available_at_utc=available_at,
        )
    )


def _registry(tmp_path) -> ScientificRegistry:
    return ScientificRegistry.initialize_pristine(tmp_path / "registry.json")


def _authority(registry: ScientificRegistry) -> DatasetSnapshotLineageAuthority:
    return DatasetSnapshotLineageAuthority.initialize_pristine(
        dataset_lineage_authority_path(registry),
        registry,
    )


def test_policy_deployment_consumes_exact_append_only_lineage_proof(tmp_path) -> None:
    registry = _registry(tmp_path)
    training_members = _members("a", "b")
    deployment_members = _members("a", "b", "c")
    training_sha = _append_snapshot(
        registry,
        snapshot_id="training",
        members=training_members,
        cutoff="2026-09-20T01:00:00Z",
        available_at="2026-09-20T01:01:00Z",
    )
    deployment_sha = _append_snapshot(
        registry,
        snapshot_id="deployment",
        members=deployment_members,
        cutoff="2026-09-20T02:00:00Z",
        available_at="2026-09-20T02:01:00Z",
    )
    authority = _authority(registry)
    authority.register(snapshot_id="training", member_sha256=training_members)
    descendant = authority.register(
        snapshot_id="deployment",
        member_sha256=deployment_members,
        parent_snapshot_id="training",
    )

    resolved = _require_dataset_lineage(
        registry,
        training_snapshot_id="training",
        deployment_snapshot_id="deployment",
        training_record_sha256=training_sha,
        deployment_record_sha256=deployment_sha,
        expected_proof_sha256=descendant.proof_sha256,
        activation_at="2099-01-01T00:00:00Z",
    )

    assert resolved.proof_sha256 == descendant.proof_sha256
    assert resolved.dataset_record_sha256 == deployment_sha


def test_same_source_and_later_cutoff_without_ancestry_fails_closed(tmp_path) -> None:
    registry = _registry(tmp_path)
    training_members = _members("a", "b")
    unrelated_members = _members("x", "y", "z")
    training_sha = _append_snapshot(
        registry,
        snapshot_id="training",
        members=training_members,
        cutoff="2026-09-20T01:00:00Z",
        available_at="2026-09-20T01:01:00Z",
    )
    unrelated_sha = _append_snapshot(
        registry,
        snapshot_id="unrelated",
        members=unrelated_members,
        cutoff="2026-09-20T03:00:00Z",
        available_at="2026-09-20T03:01:00Z",
    )
    authority = _authority(registry)
    authority.register(snapshot_id="training", member_sha256=training_members)

    with pytest.raises(
        PolicyDeploymentError,
        match="append-only ancestry is not durably proven",
    ):
        _require_dataset_lineage(
            registry,
            training_snapshot_id="training",
            deployment_snapshot_id="unrelated",
            training_record_sha256=training_sha,
            deployment_record_sha256=unrelated_sha,
            expected_proof_sha256="f" * 64,
            activation_at="2099-01-01T00:00:00Z",
        )


def test_activation_must_bind_exact_descendant_proof_digest(tmp_path) -> None:
    registry = _registry(tmp_path)
    training_members = _members("a")
    deployment_members = _members("a", "b")
    training_sha = _append_snapshot(
        registry,
        snapshot_id="training",
        members=training_members,
        cutoff="2026-09-20T01:00:00Z",
        available_at="2026-09-20T01:01:00Z",
    )
    deployment_sha = _append_snapshot(
        registry,
        snapshot_id="deployment",
        members=deployment_members,
        cutoff="2026-09-20T02:00:00Z",
        available_at="2026-09-20T02:01:00Z",
    )
    authority = _authority(registry)
    authority.register(snapshot_id="training", member_sha256=training_members)
    authority.register(
        snapshot_id="deployment",
        member_sha256=deployment_members,
        parent_snapshot_id="training",
    )

    with pytest.raises(
        PolicyDeploymentError,
        match="dataset lineage proof mismatch",
    ):
        _require_dataset_lineage(
            registry,
            training_snapshot_id="training",
            deployment_snapshot_id="deployment",
            training_record_sha256=training_sha,
            deployment_record_sha256=deployment_sha,
            expected_proof_sha256="f" * 64,
            activation_at="2099-01-01T00:00:00Z",
        )



def test_lineage_published_after_activation_cannot_retro_authorize(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autosport.dataset_snapshot_lineage as lineage_module

    registry = _registry(tmp_path)
    training_members = _members("a")
    deployment_members = _members("a", "b")
    training_sha = _append_snapshot(
        registry,
        snapshot_id="training-late-proof",
        members=training_members,
        cutoff="2026-09-20T01:00:00Z",
        available_at="2026-09-20T01:01:00Z",
    )
    deployment_sha = _append_snapshot(
        registry,
        snapshot_id="deployment-late-proof",
        members=deployment_members,
        cutoff="2026-09-20T02:00:00Z",
        available_at="2026-09-20T02:01:00Z",
    )
    monkeypatch.setattr(
        lineage_module,
        "_authority_now_utc",
        lambda: "2026-09-20T03:00:00Z",
    )
    authority = _authority(registry)
    authority.register(
        snapshot_id="training-late-proof", member_sha256=training_members
    )
    descendant = authority.register(
        snapshot_id="deployment-late-proof",
        member_sha256=deployment_members,
        parent_snapshot_id="training-late-proof",
    )

    with pytest.raises(
        PolicyDeploymentError,
        match="append-only ancestry is not durably proven",
    ):
        _require_dataset_lineage(
            registry,
            training_snapshot_id="training-late-proof",
            deployment_snapshot_id="deployment-late-proof",
            training_record_sha256=training_sha,
            deployment_record_sha256=deployment_sha,
            expected_proof_sha256=descendant.proof_sha256,
            activation_at="2026-09-20T02:30:00Z",
        )


def test_pre_activation_lineage_publication_survives_restart_and_retry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autosport.dataset_snapshot_lineage as lineage_module

    registry = _registry(tmp_path)
    training_members = _members("a")
    deployment_members = _members("a", "b")
    training_sha = _append_snapshot(
        registry,
        snapshot_id="training-early-proof",
        members=training_members,
        cutoff="2026-09-20T01:00:00Z",
        available_at="2026-09-20T01:01:00Z",
    )
    deployment_sha = _append_snapshot(
        registry,
        snapshot_id="deployment-early-proof",
        members=deployment_members,
        cutoff="2026-09-20T02:00:00Z",
        available_at="2026-09-20T02:01:00Z",
    )
    monkeypatch.setattr(
        lineage_module,
        "_authority_now_utc",
        lambda: "2026-09-20T02:10:00Z",
    )
    authority = _authority(registry)
    authority.register(
        snapshot_id="training-early-proof", member_sha256=training_members
    )
    descendant = authority.register(
        snapshot_id="deployment-early-proof",
        member_sha256=deployment_members,
        parent_snapshot_id="training-early-proof",
    )
    assert descendant.proof_registered_at == "2026-09-20T02:10:00Z"

    resolved = _require_dataset_lineage(
        registry,
        training_snapshot_id="training-early-proof",
        deployment_snapshot_id="deployment-early-proof",
        training_record_sha256=training_sha,
        deployment_record_sha256=deployment_sha,
        expected_proof_sha256=descendant.proof_sha256,
        activation_at="2026-09-20T02:30:00Z",
    )
    assert resolved == descendant

    monkeypatch.setattr(
        lineage_module,
        "_authority_now_utc",
        lambda: (_ for _ in ()).throw(AssertionError("retry must preserve publication time")),
    )
    restarted = DatasetSnapshotLineageAuthority(
        dataset_lineage_authority_path(registry), registry
    )
    assert restarted.register(
        snapshot_id="deployment-early-proof",
        member_sha256=deployment_members,
        parent_snapshot_id="training-early-proof",
    ) == descendant
