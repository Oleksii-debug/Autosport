from __future__ import annotations

import pytest

import autosport.risk_membership_publication as publication
from autosport.risk_sampling_membership import ResolvedFixedNRiskMembership
import autosport.risk_randomization_precommit as precommit


def _membership() -> ResolvedFixedNRiskMembership:
    return ResolvedFixedNRiskMembership(
        research_protocol_id="protocol-001",
        protocol_sha256="d" * 64,
        protocol_record_sha256="e" * 64,
        dataset_snapshot_id="dataset-001",
        dataset_manifest_sha256="f" * 64,
        dataset_record_sha256="1" * 64,
        causal_cutoff="2026-09-23T00:00:00+00:00",
        outcome_reveal_after="2026-09-24T00:00:00+00:00",
        precommitted_at="2026-09-23T00:10:00+00:00",
        planned_run_ids=("run-001", "run-002"),
        sampling_frame_sha256="2" * 64,
        design_sha256="3" * 64,
    )


def test_public_entropy_dispatch_rebinding_fails_closed_before_root_publication(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = workspace / "scientific-registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    authority_root = tmp_path / "machine-authority"
    membership = _membership()
    monkeypatch.setattr(
        publication,
        "inspect_fixed_n_risk_membership_structure",
        lambda *_args, **_kwargs: membership,
    )
    receipt = publication.publish_fixed_n_membership_structure(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        authority_root=authority_root,
    )

    attacker_entropy = b"\x00" * 32
    monkeypatch.setattr(
        precommit.secrets,
        "token_bytes",
        lambda length: attacker_entropy[:length],
    )

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="randomization entropy source was rebound",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=receipt.research_protocol_id,
            dataset_snapshot_id=receipt.dataset_snapshot_id,
            experiment_id="experiment-entropy-rebind",
            authority_root=authority_root,
        )

    assert not list(workspace.glob(".risk-randomization-precommit-*.json"))
