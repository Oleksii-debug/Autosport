from __future__ import annotations

import pytest

import autosport.external_validity_policy_issuance as issuance
from autosport.monotonic_workspace_binding import WorkspaceIdentityBinding
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


def _bound_workspace(tmp_path, monkeypatch):
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )
    workspace = (tmp_path / "canonical").resolve()
    binding = WorkspaceIdentityBinding.resolve(
        workspace=workspace,
        authority_root=authority_root,
        requested_workspace_instance_id=None,
    )
    binding.ensure_bound()
    ScientificRegistry.initialize_pristine(workspace / "scientific_registry.json")
    FactoryArtifactStore(workspace / "factory-artifacts")
    authority = issuance.ProductPolicyEvaluationWorkspace.open(
        workspace,
        expected_workspace_instance_id=binding.workspace_instance_id,
    )
    return workspace, authority


def test_registry_constructor_rebinding_cannot_redirect_canonical_authority(
    tmp_path,
    monkeypatch,
):
    workspace, authority = _bound_workspace(tmp_path, monkeypatch)
    attacker_registry_path = (tmp_path / "attacker-registry.json").resolve()
    ScientificRegistry.initialize_pristine(attacker_registry_path)

    original_registry_type = issuance.ScientificRegistry

    class RedirectedRegistry:
        get = original_registry_type.get
        append = original_registry_type.append

        def __init__(self, _canonical_path):
            self.path = attacker_registry_path

    monkeypatch.setattr(issuance, "ScientificRegistry", RedirectedRegistry)

    with pytest.raises(issuance.ProductPolicyEvaluationIssuanceError):
        issuance._open_canonical_authorities(authority)

    assert authority.workspace == workspace


def test_store_constructor_rebinding_cannot_redirect_canonical_authority(
    tmp_path,
    monkeypatch,
):
    workspace, authority = _bound_workspace(tmp_path, monkeypatch)
    attacker_store_root = (tmp_path / "attacker-artifacts").resolve()
    attacker_store = FactoryArtifactStore(attacker_store_root)

    original_store_type = issuance.FactoryArtifactStore

    class RedirectedStore:
        read = original_store_type.read
        sha256 = original_store_type.sha256
        materialize = original_store_type.materialize
        materialization_receipt = original_store_type.materialization_receipt

        def __init__(self, _canonical_root):
            self.root = attacker_store_root
            self._clock = attacker_store._clock

    monkeypatch.setattr(issuance, "FactoryArtifactStore", RedirectedStore)

    with pytest.raises(issuance.ProductPolicyEvaluationIssuanceError):
        issuance._open_canonical_authorities(authority)

    assert authority.workspace == workspace
