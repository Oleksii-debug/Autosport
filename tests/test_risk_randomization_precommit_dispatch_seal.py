from __future__ import annotations

from types import SimpleNamespace

import pytest

import autosport._risk_randomization_precommit_internal_guard as dispatch_guard
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


def _paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = workspace / "scientific-registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    authority_root = tmp_path / "machine-authority"
    return workspace, registry, authority_root


def _install_membership(monkeypatch) -> RiskMembershipPublicationReceipt:
    receipt = _membership_receipt()
    monkeypatch.setattr(
        precommit,
        "resolve_fixed_n_membership_publication",
        lambda *_args, **_kwargs: receipt,
    )
    return receipt


def test_guard_does_not_expose_mutable_target_module_binding() -> None:
    assert not hasattr(dispatch_guard, "_precommit")


def test_hashlib_module_rebinding_fails_before_randomization_publication(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(monkeypatch)

    class _ForgedDigest:
        def hexdigest(self) -> str:
            return "0" * 64

    monkeypatch.setattr(
        precommit,
        "hashlib",
        SimpleNamespace(sha256=lambda _payload=b"": _ForgedDigest()),
    )

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="cryptographic digest dispatch was rebound",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id="experiment-hash-rebind",
            authority_root=authority_root,
        )

    assert not list(workspace.glob(".risk-randomization-precommit-*.json"))


def test_root_size_rebinding_fails_before_randomization_publication(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(monkeypatch)
    monkeypatch.setattr(precommit, "_ROOT_BYTES", 1)

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="root size authority was rebound",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id="experiment-root-size-rebind",
            authority_root=authority_root,
        )

    assert not list(workspace.glob(".risk-randomization-precommit-*.json"))


def test_authority_hash_helpers_are_private_snapshots(tmp_path, monkeypatch) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(monkeypatch)

    def forged_binding(**_kwargs):
        raise AssertionError("public helper rebinding reached authority path")

    def forged_json(*_args, **_kwargs):
        raise AssertionError("public json rebinding reached authority path")

    monkeypatch.setattr(precommit, "_semantic_binding_sha256", forged_binding)
    monkeypatch.setattr(precommit, "json", SimpleNamespace(dumps=forged_json))

    issued = precommit.issue_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id="experiment-private-hash-dispatch",
        authority_root=authority_root,
    )

    assert len(issued.randomization_root_sha256) == 64
    assert issued.randomization_root_sha256 != "0" * 64
    assert issued.product_randomization_root_issued is True
    assert issued.iid_qualified is False
    assert issued.grants_real_money_authority is False
