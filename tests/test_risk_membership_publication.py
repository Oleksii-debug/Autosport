from __future__ import annotations

import json
import os
import shutil

import pytest

from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.risk_sampling_membership import ResolvedFixedNRiskMembership
import autosport.risk_membership_publication as publication


def _membership(
    *,
    run_ids: tuple[str, ...] = ("run-001", "run-002"),
) -> ResolvedFixedNRiskMembership:
    return ResolvedFixedNRiskMembership(
        research_protocol_id="protocol-001",
        protocol_sha256="1" * 64,
        protocol_record_sha256="2" * 64,
        dataset_snapshot_id="dataset-001",
        dataset_manifest_sha256="3" * 64,
        dataset_record_sha256="4" * 64,
        causal_cutoff="2026-09-23T00:00:00Z",
        outcome_reveal_after="2026-09-24T00:00:00Z",
        precommitted_at="2026-09-23T00:10:00Z",
        planned_run_ids=run_ids,
        sampling_frame_sha256="5" * 64,
        design_sha256="6" * 64,
    )


def _paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = workspace / "scientific-registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    authority_root = tmp_path / "machine-authority"
    return workspace, registry, authority_root


def _install_membership(monkeypatch, value: ResolvedFixedNRiskMembership) -> None:
    monkeypatch.setattr(
        publication,
        "inspect_fixed_n_risk_membership_structure",
        lambda *_args, **_kwargs: value,
    )


def _publish(tmp_path, monkeypatch, value=None):
    workspace, registry, authority_root = _paths(tmp_path)
    resolved = value or _membership()
    _install_membership(monkeypatch, resolved)
    receipt = publication.publish_fixed_n_membership_structure(
        registry,
        workspace=workspace,
        research_protocol_id=resolved.research_protocol_id,
        dataset_snapshot_id=resolved.dataset_snapshot_id,
        authority_root=authority_root,
    )
    return receipt, workspace, registry, authority_root, resolved


def test_publication_is_durable_but_not_causal_or_iid_authority(
    tmp_path, monkeypatch
) -> None:
    receipt, *_ = _publish(tmp_path, monkeypatch)

    assert not hasattr(receipt, "publication_proven")
    assert receipt.causal_precommit_proven is False
    assert receipt.member_run_ancestry_proven is False
    assert receipt.historical_outcome_unavailability_proven is False
    assert receipt.iid_qualified is False
    assert receipt.planned_run_ids == ("run-001", "run-002")
    assert len(receipt.membership_sha256) == 64
    assert len(receipt.receipt_sha256) == 64


def test_exact_retry_reuses_same_committed_receipt(tmp_path, monkeypatch) -> None:
    first, workspace, registry, authority_root, resolved = _publish(
        tmp_path, monkeypatch
    )
    second = publication.publish_fixed_n_membership_structure(
        registry,
        workspace=workspace,
        research_protocol_id=resolved.research_protocol_id,
        dataset_snapshot_id=resolved.dataset_snapshot_id,
        authority_root=authority_root,
    )
    verified = publication.resolve_fixed_n_membership_publication(
        registry,
        workspace=workspace,
        research_protocol_id=resolved.research_protocol_id,
        dataset_snapshot_id=resolved.dataset_snapshot_id,
        authority_root=authority_root,
    )

    assert second == first
    assert verified == first
    assert second.authority_generation == first.authority_generation


def test_restart_recovers_published_state_after_crash_before_commit(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    resolved = _membership()
    _install_membership(monkeypatch, resolved)

    original_commit = MonotonicWorkspaceAuthority.commit

    def crash_before_commit(self, **_kwargs):
        raise OSError("simulated crash after local publication")

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", crash_before_commit)
    with pytest.raises(publication.RiskMembershipPublicationError):
        publication.publish_fixed_n_membership_structure(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=authority_root,
        )

    with pytest.raises(
        publication.RiskMembershipPublicationError,
        match="pending PREPARE",
    ):
        publication.resolve_fixed_n_membership_publication(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=authority_root,
        )

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", original_commit)
    recovered = publication.publish_fixed_n_membership_structure(
        registry,
        workspace=workspace,
        research_protocol_id=resolved.research_protocol_id,
        dataset_snapshot_id=resolved.dataset_snapshot_id,
        authority_root=authority_root,
    )

    assert recovered.authority_generation == 1


def test_local_state_deletion_after_commit_is_detected_as_rollback(
    tmp_path, monkeypatch
) -> None:
    receipt, workspace, registry, authority_root, resolved = _publish(
        tmp_path, monkeypatch
    )
    state = workspace / f".risk-fixed-n-membership-{receipt.membership_sha256}.json"
    state.unlink()

    with pytest.raises(
        publication.RiskMembershipPublicationError,
        match="failed closed",
    ):
        publication.publish_fixed_n_membership_structure(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=authority_root,
        )


def test_state_tamper_cannot_be_reinterpreted_as_same_receipt(
    tmp_path, monkeypatch
) -> None:
    receipt, workspace, registry, authority_root, resolved = _publish(
        tmp_path, monkeypatch
    )
    state = workspace / f".risk-fixed-n-membership-{receipt.membership_sha256}.json"
    raw = json.loads(state.read_text(encoding="utf-8"))
    raw["membership"]["planned_run_ids"] = ["run-001"]
    state.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        publication.RiskMembershipPublicationError,
        match="conflicts with exact membership",
    ):
        publication.publish_fixed_n_membership_structure(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=authority_root,
        )


def test_semantically_equal_but_noncanonical_state_bytes_fail_closed(
    tmp_path, monkeypatch
) -> None:
    receipt, workspace, registry, authority_root, resolved = _publish(
        tmp_path, monkeypatch
    )
    state = workspace / f".risk-fixed-n-membership-{receipt.membership_sha256}.json"
    raw = json.loads(state.read_text(encoding="utf-8"))
    state.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")

    with pytest.raises(
        publication.RiskMembershipPublicationError,
        match="not canonically serialized",
    ):
        publication.publish_fixed_n_membership_structure(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=authority_root,
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require Windows privilege")
def test_state_symlink_substitution_fails_closed(tmp_path, monkeypatch) -> None:
    receipt, workspace, registry, authority_root, resolved = _publish(
        tmp_path, monkeypatch
    )
    state = workspace / f".risk-fixed-n-membership-{receipt.membership_sha256}.json"
    target = workspace / "moved-state.json"
    state.rename(target)
    state.symlink_to(target.name)

    with pytest.raises(
        publication.RiskMembershipPublicationError,
        match="must be one regular file",
    ):
        publication.publish_fixed_n_membership_structure(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=authority_root,
        )


def test_read_only_resolver_does_not_create_missing_receipt(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    resolved = _membership()
    _install_membership(monkeypatch, resolved)

    with pytest.raises(
        publication.RiskMembershipPublicationError,
        match="no existing publication receipt",
    ):
        publication.resolve_fixed_n_membership_publication(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=authority_root,
        )

    assert not list(workspace.glob(".risk-fixed-n-membership-*.json"))
    assert not authority_root.exists()


def test_registry_outside_workspace_is_rejected_before_publication(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "outside-registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    resolved = _membership()
    _install_membership(monkeypatch, resolved)

    with pytest.raises(
        publication.RiskMembershipPublicationError,
        match="must be inside the protected workspace",
    ):
        publication.publish_fixed_n_membership_structure(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=tmp_path / "machine-authority",
        )


def test_missing_machine_history_cannot_adopt_existing_local_receipt(
    tmp_path, monkeypatch
) -> None:
    receipt, workspace, registry, authority_root, resolved = _publish(
        tmp_path, monkeypatch
    )

    shutil.rmtree(authority_root)

    with pytest.raises(
        publication.RiskMembershipPublicationError,
        match="failed closed",
    ):
        publication.publish_fixed_n_membership_structure(
            registry,
            workspace=workspace,
            research_protocol_id=resolved.research_protocol_id,
            dataset_snapshot_id=resolved.dataset_snapshot_id,
            authority_root=authority_root,
        )
