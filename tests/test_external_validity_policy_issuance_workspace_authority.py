from __future__ import annotations

import inspect

import pytest

from autosport.external_validity_policy_issuance import (
    IssuedPolicyEvaluationRef,
    ProductPolicyEvaluationIssuanceError,
    ProductPolicyEvaluationWorkspace,
    issue_product_policy_evaluation,
    resolve_product_policy_evaluation,
    verify_product_policy_evaluation,
)
from autosport.monotonic_workspace_binding import WorkspaceIdentityBinding
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


def _bind_workspace(tmp_path, monkeypatch, name: str):
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )
    workspace = (tmp_path / name).resolve()
    binding = WorkspaceIdentityBinding.resolve(
        workspace=workspace,
        authority_root=authority_root,
        requested_workspace_instance_id=None,
    )
    binding.ensure_bound()
    ScientificRegistry.initialize_pristine(workspace / "scientific_registry.json")
    FactoryArtifactStore(workspace / "factory-artifacts")
    return workspace, binding.workspace_instance_id


def test_workspace_authority_requires_preexisting_durable_product_binding(
    tmp_path,
    monkeypatch,
):
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )
    workspace = (tmp_path / "unbound").resolve()

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="durably bound before issuance",
    ):
        ProductPolicyEvaluationWorkspace.open(
            workspace,
            expected_workspace_instance_id="expected-product-workspace",
        )

    assert not (workspace / "scientific_registry.json").exists()
    assert not (workspace / "factory-artifacts").exists()


def test_workspace_authority_reopens_exact_bound_product_workspace(
    tmp_path,
    monkeypatch,
):
    workspace, workspace_id = _bind_workspace(tmp_path, monkeypatch, "canonical")

    first = ProductPolicyEvaluationWorkspace.open(
        workspace,
        expected_workspace_instance_id=workspace_id,
    )
    second = ProductPolicyEvaluationWorkspace.open(
        workspace,
        expected_workspace_instance_id=workspace_id,
    )

    assert first == second
    assert first.workspace == workspace
    assert first.workspace_instance_id == workspace_id
    assert len(first.workspace_locator_sha256) == 64


def test_copied_or_fresh_workspace_cannot_reuse_canonical_instance_identity(
    tmp_path,
    monkeypatch,
):
    canonical, workspace_id = _bind_workspace(tmp_path, monkeypatch, "canonical")
    assert ProductPolicyEvaluationWorkspace.open(
        canonical,
        expected_workspace_instance_id=workspace_id,
    ).workspace_instance_id == workspace_id

    attacker = (tmp_path / "attacker").resolve()
    ScientificRegistry.initialize_pristine(attacker / "scientific_registry.json")
    FactoryArtifactStore(attacker / "factory-artifacts")

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="durably bound before issuance",
    ):
        ProductPolicyEvaluationWorkspace.open(
            attacker,
            expected_workspace_instance_id=workspace_id,
        )

    # Even if another component deliberately registers a copied/moved path with
    # the same durable instance id, locator identity keeps the two roots distinct.
    authority_root = (tmp_path / "machine-authority").resolve()
    attacker_binding = WorkspaceIdentityBinding.resolve(
        workspace=attacker,
        authority_root=authority_root,
        requested_workspace_instance_id=workspace_id,
    )
    attacker_binding.ensure_bound()
    canonical_authority = ProductPolicyEvaluationWorkspace.open(
        canonical,
        expected_workspace_instance_id=workspace_id,
    )
    attacker_authority = ProductPolicyEvaluationWorkspace.open(
        attacker,
        expected_workspace_instance_id=workspace_id,
    )
    assert (
        canonical_authority.workspace_locator_sha256
        != attacker_authority.workspace_locator_sha256
    )

    copied_reference = IssuedPolicyEvaluationRef(
        issuance_id="a" * 64,
        workspace_instance_id=workspace_id,
        workspace_locator_sha256=canonical_authority.workspace_locator_sha256,
        evaluation_bundle_id="external-validity-policy-result:" + "a" * 64,
        evaluation_bundle_sha256="b" * 64,
        result_artifact_sha256="c" * 64,
        source_evaluation_bundle_id="source-evaluation",
        source_evaluation_bundle_sha256="d" * 64,
        policy_id="policy",
    )
    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="belongs to another product workspace",
    ):
        resolve_product_policy_evaluation(
            attacker_authority,
            None,  # rejected on workspace identity before protocol parsing
            copied_reference,
        )


def test_public_issuance_api_has_no_registry_store_or_clock_injection_surface():
    for function in (
        issue_product_policy_evaluation,
        resolve_product_policy_evaluation,
        verify_product_policy_evaluation,
    ):
        parameters = inspect.signature(function).parameters
        assert "authority" in parameters
        assert "registry" not in parameters
        assert "artifact_store" not in parameters
        assert "clock" not in parameters
