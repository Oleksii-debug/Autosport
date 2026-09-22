from __future__ import annotations

from types import SimpleNamespace

import pytest

from autosport import resolver_semantics
from autosport import run_transaction as run_transaction_module
from autosport.external_validity_policy_issuance import (
    ProductPolicyEvaluationIssuanceError,
    _store_read,
)
from autosport.run_transaction import RunTransaction, VerifiedFileSnapshot
from autosport.strategy_model_factory import FactoryArtifactStore


def test_reader_guard_rejects_code_mutation_when_semantic_helpers_are_rebound(
    tmp_path,
    monkeypatch,
):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    reader = RunTransaction._read_canonical_file_snapshot

    def forged_reader(path, label):
        del path, label
        return VerifiedFileSnapshot(
            payload=b'{"kind":"caller-forged-evaluation"}',
            sha256="d" * 64,
        )

    monkeypatch.setattr(reader, "__code__", forged_reader.__code__)
    monkeypatch.setattr(
        resolver_semantics,
        "_module_source",
        lambda candidate: "",
    )
    monkeypatch.setattr(
        resolver_semantics,
        "_qualname_parts",
        lambda candidate: (),
    )
    monkeypatch.setattr(
        resolver_semantics,
        "_compiled_resolver_code",
        lambda source, parts: reader.__code__,
    )
    monkeypatch.setattr(
        resolver_semantics,
        "_code_payload",
        lambda code: "caller-controlled-equality",
    )

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore executable authority was rebound",
    ):
        _store_read(
            store,
            "evaluation",
            "caller-forged-evaluation",
            expected_sha256="d" * 64,
        )


def test_reader_guard_rejects_hashlib_binding_rebind_before_tampered_read(
    tmp_path,
    monkeypatch,
):
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    identity = "canonical-reader-global-dependency"
    original_payload = {
        "schema_version": 1,
        "kind": "canonical-policy-evaluation",
    }
    expected_sha256 = store.write("evaluation", identity, original_payload)

    artifact_path = store.path_for_testing("evaluation", identity)
    artifact_path.write_text(
        '{"schema_version":1,"kind":"caller-forged-evaluation"}\n',
        encoding="utf-8",
    )

    class _ForgedDigest:
        def hexdigest(self):
            return expected_sha256

    forged_hashlib = SimpleNamespace(
        sha256=lambda payload: _ForgedDigest(),
    )
    monkeypatch.setattr(
        run_transaction_module,
        "hashlib",
        forged_hashlib,
    )

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore executable authority was rebound",
    ):
        _store_read(
            store,
            "evaluation",
            identity,
            expected_sha256=expected_sha256,
        )
