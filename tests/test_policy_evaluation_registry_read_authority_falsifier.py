from __future__ import annotations

from types import SimpleNamespace

import pytest

from autosport.external_validity_policy_issuance import (
    ProductPolicyEvaluationIssuanceError,
    _registry_get,
    _store_read,
)
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


def test_product_issuer_rejects_scientific_registry_class_read_rebind(
    tmp_path,
    monkeypatch,
):
    registry_path = tmp_path / "scientific_registry.json"
    ScientificRegistry.initialize_pristine(registry_path)
    registry = ScientificRegistry(registry_path)
    forged_read_called = False

    def forged_read(self):
        nonlocal forged_read_called
        forged_read_called = True
        return {"schema_version": 1, "records": []}

    monkeypatch.setattr(ScientificRegistry, "_read", forged_read)

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="ScientificRegistry executable authority was rebound",
    ):
        _registry_get(registry, "DatasetSnapshot", "caller-forged-dataset")

    assert forged_read_called is False


def test_product_issuer_rejects_factory_store_snapshot_reader_rebind(
    tmp_path,
    monkeypatch,
):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    forged_snapshot_called = False

    def forged_snapshot(self, kind, identity):
        nonlocal forged_snapshot_called
        forged_snapshot_called = True
        return SimpleNamespace(
            payload=b'{"kind":"caller-forged-evaluation"}',
            sha256="a" * 64,
        )

    monkeypatch.setattr(FactoryArtifactStore, "_stable_snapshot", forged_snapshot)

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore executable authority was rebound",
    ):
        _store_read(
            store,
            "evaluation",
            "caller-forged-evaluation",
            expected_sha256="a" * 64,
        )

    assert forged_snapshot_called is False


def test_product_issuer_rejects_scientific_registry_validator_rebind(
    tmp_path,
    monkeypatch,
):
    registry_path = tmp_path / "scientific_registry.json"
    ScientificRegistry.initialize_pristine(registry_path)
    registry = ScientificRegistry(registry_path)
    forged_validator_called = False

    def forged_validator(raw_entry):
        nonlocal forged_validator_called
        forged_validator_called = True

    monkeypatch.setattr(ScientificRegistry, "_validate_entry", forged_validator)

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="ScientificRegistry executable authority was rebound",
    ):
        _registry_get(registry, "DatasetSnapshot", "caller-forged-dataset")

    assert forged_validator_called is False


def test_product_issuer_rejects_factory_store_decoder_rebind(
    tmp_path,
    monkeypatch,
):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    forged_decoder_called = False

    def forged_decoder(snapshot, kind, identity):
        nonlocal forged_decoder_called
        forged_decoder_called = True
        return {"kind": "caller-forged-evaluation"}

    monkeypatch.setattr(FactoryArtifactStore, "_decode_snapshot", forged_decoder)

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore executable authority was rebound",
    ):
        _store_read(
            store,
            "evaluation",
            "caller-forged-evaluation",
            expected_sha256="a" * 64,
        )

    assert forged_decoder_called is False


def test_product_issuer_rejects_factory_store_path_rebind(
    tmp_path,
    monkeypatch,
):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    forged_path_called = False

    def forged_path(self, kind, identity):
        nonlocal forged_path_called
        forged_path_called = True
        return tmp_path / "caller-controlled.json"

    monkeypatch.setattr(FactoryArtifactStore, "_path", forged_path)

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore executable authority was rebound",
    ):
        _store_read(
            store,
            "evaluation",
            "caller-forged-evaluation",
            expected_sha256="a" * 64,
        )

    assert forged_path_called is False

