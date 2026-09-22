from __future__ import annotations

from types import SimpleNamespace

import pytest

from autosport.external_validity_policy_issuance import (
    ProductPolicyEvaluationIssuanceError,
    _store_read,
)
from autosport.run_transaction import RunTransaction
from autosport.strategy_model_factory import FactoryArtifactStore


def test_product_issuer_rejects_canonical_file_snapshot_reader_rebind(
    tmp_path,
    monkeypatch,
):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    forged_reader_called = False

    def forged_reader(path, label):
        nonlocal forged_reader_called
        del path, label
        forged_reader_called = True
        return SimpleNamespace(
            payload=b'{"kind":"caller-forged-evaluation"}',
            sha256="a" * 64,
        )

    monkeypatch.setattr(
        RunTransaction,
        "_read_canonical_file_snapshot",
        staticmethod(forged_reader),
    )

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

    assert forged_reader_called is False
