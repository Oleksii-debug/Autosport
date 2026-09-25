from __future__ import annotations

import pytest

from autosport.risk_membership_publication import RiskMembershipPublicationReceipt
import autosport.risk_randomization_precommit as precommit


def _membership_receipt() -> RiskMembershipPublicationReceipt:
    return RiskMembershipPublicationReceipt(
        workspace_instance_id="workspace-test",
        membership_sha256="a" * 64,
        authority_generation=1,
        authority_record_sha256="b" * 64,
        state_sha256="c" * 64,
        research_protocol_id="protocol-001",
        protocol_sha256="d" * 64,
        protocol_record_sha256="e" * 64,
        dataset_snapshot_id="dataset-001",
        dataset_manifest_sha256="f" * 64,
        dataset_record_sha256="1" * 64,
        causal_cutoff="2026-09-23T00:00:00+00:00",
        outcome_reveal_after="2026-09-24T00:00:00+00:00",
        planned_run_ids=("run-001", "run-002"),
        sampling_frame_sha256="2" * 64,
        design_sha256="3" * 64,
        receipt_sha256="4" * 64,
    )


def test_public_entropy_dispatch_rebinding_fails_closed_before_root_publication(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = workspace / "scientific-registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    authority_root = tmp_path / "machine-authority"
    receipt = _membership_receipt()

    monkeypatch.setattr(
        precommit,
        "resolve_fixed_n_membership_publication",
        lambda *_args, **_kwargs: receipt,
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
