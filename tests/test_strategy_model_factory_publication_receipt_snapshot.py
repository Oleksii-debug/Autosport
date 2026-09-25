from __future__ import annotations

import json

import pytest

import autosport.strategy_model_factory as factory_module
from autosport.integrity import atomic_write_json
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


SHA_A = "a" * 64
T0 = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolated_publish_authority(tmp_path, monkeypatch):
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root))


def test_publication_receipt_rejects_artifact_substitution_during_snapshot(
    tmp_path,
    monkeypatch,
):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    original_state = registry._read()
    registry.append(
        ResearchQuestion(
            "question-publication-snapshot",
            "Are these exact executable bytes still bound to the publication receipt?",
            SHA_A,
            T0,
        )
    )
    final_state = registry._read()

    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )
    transaction = {
        "schema_version": 1,
        "phase": "prepared",
        "original_registry_sha256": factory_module._registry_state_sha256(
            original_state
        ),
        "final_registry_sha256": factory_module._registry_state_sha256(final_state),
        "artifacts": [
            {
                "kind": "model",
                "identity": "model-v2",
                "sha256": artifact_sha256,
            }
        ],
    }
    atomic_write_json(factory_module._publish_transaction_path(registry), transaction)
    factory_module._record_committed_factory_publish(registry, store)

    artifact_path = store.path_for_testing("model", "model-v2")
    canonical_sha256 = FactoryArtifactStore.sha256
    hash_calls = 0

    def substitute_after_first_hash(self, kind, identity):
        nonlocal hash_calls
        digest = canonical_sha256(self, kind, identity)
        if kind == "model" and identity == "model-v2" and hash_calls == 0:
            hash_calls += 1
            artifact_path.write_text(
                json.dumps(
                    {"schema_version": 1, "model_id": "substituted-model"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        else:
            hash_calls += 1
        return digest

    monkeypatch.setattr(
        FactoryArtifactStore,
        "sha256",
        substitute_after_first_hash,
    )

    with pytest.raises(
        ValueError,
        match="artifact changed during publication receipt verification",
    ):
        store.publication_receipt(
            "model",
            "model-v2",
            expected_sha256=artifact_sha256,
        )

    assert hash_calls >= 2
