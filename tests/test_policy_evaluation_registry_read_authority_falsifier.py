from __future__ import annotations

import pytest

from autosport.external_validity_policy_issuance import (
    ProductPolicyEvaluationIssuanceError,
    _registry_get,
)
from autosport.scientific_registry import ScientificRegistry


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
