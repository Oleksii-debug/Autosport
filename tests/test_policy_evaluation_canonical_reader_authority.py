from __future__ import annotations

from types import SimpleNamespace

import pytest

from autosport.external_validity_policy_issuance import (
    ProductPolicyEvaluationIssuanceError,
    _store_read,
)
from autosport.run_transaction import RunTransaction, VerifiedFileSnapshot
from autosport import strategy_model_factory as strategy_model_factory_module
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


def test_product_issuer_rejects_strategy_factory_run_transaction_symbol_rebind(
    tmp_path,
    monkeypatch,
):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    forged_reader_called = False

    class ForgedRunTransaction:
        @staticmethod
        def _read_canonical_file_snapshot(path, label):
            nonlocal forged_reader_called
            del path, label
            forged_reader_called = True
            return SimpleNamespace(
                payload=b'{"kind":"caller-forged-evaluation"}',
                sha256="b" * 64,
            )

    monkeypatch.setattr(
        strategy_model_factory_module,
        "RunTransaction",
        ForgedRunTransaction,
    )

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore executable authority was rebound",
    ):
        _store_read(
            store,
            "evaluation",
            "caller-forged-evaluation",
            expected_sha256="b" * 64,
        )

    assert forged_reader_called is False


def test_product_issuer_rejects_in_place_canonical_reader_code_mutation(
    tmp_path,
    monkeypatch,
):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    reader = RunTransaction._read_canonical_file_snapshot

    def forged_reader(path, label):
        del path, label
        return VerifiedFileSnapshot(
            payload=b'{"kind":"caller-forged-evaluation"}',
            sha256="c" * 64,
        )

    monkeypatch.setattr(reader, "__code__", forged_reader.__code__)

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore executable authority was rebound",
    ):
        _store_read(
            store,
            "evaluation",
            "caller-forged-evaluation",
            expected_sha256="c" * 64,
        )


def test_product_issuer_preserves_canonical_store_reads(tmp_path):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    payload = {
        "schema_version": 1,
        "kind": "canonical-policy-evaluation-test",
    }
    digest = store.write("evaluation", "canonical-policy-evaluation", payload)

    assert _store_read(
        store,
        "evaluation",
        "canonical-policy-evaluation",
        expected_sha256=digest,
    ) == payload
