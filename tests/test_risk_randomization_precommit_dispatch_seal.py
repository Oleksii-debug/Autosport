from __future__ import annotations

from dataclasses import replace
from types import FunctionType, SimpleNamespace

import pytest

import autosport._risk_randomization_precommit_internal_guard as dispatch_guard
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


def _paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = workspace / "scientific-registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    authority_root = tmp_path / "machine-authority"
    return workspace, registry, authority_root


def _install_membership(monkeypatch, workspace, registry, authority_root):
    membership = _membership()
    monkeypatch.setattr(
        publication,
        "inspect_fixed_n_risk_membership_structure",
        lambda *_args, **_kwargs: membership,
    )
    return publication.publish_fixed_n_membership_structure(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        authority_root=authority_root,
    )


def _frozen_implementation() -> FunctionType:
    closure = precommit.issue_risk_randomization_precommit.__closure__ or ()
    for cell in closure:
        value = cell.cell_contents
        if type(value) is FunctionType and value.__name__ == "_issue_risk_randomization_precommit":
            return value
    raise AssertionError("sealed issuer does not retain its checked implementation")


def test_guard_does_not_expose_mutable_target_module_binding() -> None:
    assert not hasattr(dispatch_guard, "_precommit")


def test_hashlib_module_rebinding_fails_before_randomization_publication(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(
        monkeypatch, workspace, registry, authority_root
    )

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
    membership = _install_membership(
        monkeypatch, workspace, registry, authority_root
    )
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


def test_direct_frozen_implementation_globals_mutation_fails_closed(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(
        monkeypatch, workspace, registry, authority_root
    )
    implementation = _frozen_implementation()
    monkeypatch.setitem(
        implementation.__globals__,
        "hashlib",
        SimpleNamespace(sha256=lambda _payload=b"": None),
    )

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match=r"frozen randomization implementation global 'hashlib' was rebound",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id="experiment-direct-clone-globals",
            authority_root=authority_root,
        )

    assert not list(workspace.glob(".risk-randomization-precommit-*.json"))


def test_private_hash_facade_attribute_mutation_fails_closed(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(
        monkeypatch, workspace, registry, authority_root
    )
    implementation = _frozen_implementation()
    private_hashlib = implementation.__globals__["hashlib"]
    monkeypatch.setattr(private_hashlib, "sha256", lambda _payload=b"": None)

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="frozen randomization SHA-256 dispatch was rebound",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id="experiment-private-facade",
            authority_root=authority_root,
        )

    assert not list(workspace.glob(".risk-randomization-precommit-*.json"))


def test_authority_hash_helpers_are_private_snapshots(tmp_path, monkeypatch) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(
        monkeypatch, workspace, registry, authority_root
    )

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


def test_membership_resolver_rebinding_cannot_mint_randomization_authority(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(
        monkeypatch, workspace, registry, authority_root
    )
    forged = replace(
        membership,
        membership_sha256="9" * 64,
        receipt_sha256="8" * 64,
    )
    attacker_called = False

    def forged_resolver(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return forged

    monkeypatch.setattr(
        precommit,
        "resolve_fixed_n_membership_publication",
        forged_resolver,
    )

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="membership resolver was rebound",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id="experiment-forged-membership",
            authority_root=authority_root,
        )

    assert attacker_called is False
    assert not list(workspace.glob(".risk-randomization-precommit-*.json"))


def test_captured_public_resolver_fails_closed_after_module_rebinding(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _install_membership(
        monkeypatch, workspace, registry, authority_root
    )
    issued = precommit.issue_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id="experiment-resolver-rebind",
        authority_root=authority_root,
    )
    captured_resolver = precommit.resolve_risk_randomization_precommit
    monkeypatch.setattr(
        precommit,
        "resolve_risk_randomization_precommit",
        lambda *_args, **_kwargs: issued,
    )

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="public resolver was rebound",
    ):
        captured_resolver(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id=issued.experiment_id,
            authority_root=authority_root,
        )
