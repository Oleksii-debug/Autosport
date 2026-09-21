from __future__ import annotations

import inspect

import pytest

from autosport.external_validity_policy_issuance import (
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
