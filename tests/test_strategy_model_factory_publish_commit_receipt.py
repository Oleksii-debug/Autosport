from datetime import datetime, timezone

import pytest

import autosport.strategy_model_factory as factory_module
from autosport.integrity import atomic_write_json
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-01-01T00:00:00+00:00"
COMMITTED_AT = datetime(2026, 1, 9, 12, 0, tzinfo=timezone.utc)


def _transaction(
    *,
    original_sha256,
    final_sha256,
    artifact_sha256,
    kind="model",
    identity="model-v2",
):
    return {
        "schema_version": 1,
        "phase": "prepared",
        "original_registry_sha256": original_sha256,
        "final_registry_sha256": final_sha256,
        "artifacts": [
            {
                "kind": kind,
                "identity": identity,
                "sha256": artifact_sha256,
            }
        ],
    }


def _final_registry_state(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    original = registry._read()
    registry.append(
        ResearchQuestion(
            "question-commit-receipt",
            "Was this executable durably available from the factory transaction?",
            SHA_A,
            T0,
        )
    )
    return registry, original, registry._read()


def test_publish_commit_receipt_binds_artifact_and_final_registry(tmp_path):
    store = FactoryArtifactStore(
        tmp_path / "factory-artifacts",
        clock=lambda: COMMITTED_AT,
    )
    artifact_sha256 = store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )
    transaction = _transaction(
        original_sha256=SHA_A,
        final_sha256=SHA_B,
        artifact_sha256=artifact_sha256,
    )

    first = store._record_publish_commit(transaction)
    second = store._record_publish_commit(transaction)
    receipt = store.publication_receipt(
        "model",
        "model-v2",
        expected_sha256=artifact_sha256,
    )

    assert first == second == receipt
    assert receipt["committed_at"] == "2026-01-09T12:00:00Z"
    assert receipt["original_registry_sha256"] == SHA_A
    assert receipt["final_registry_sha256"] == SHA_B
    assert receipt["artifacts"] == [
        {
            "kind": "model",
            "identity": "model-v2",
            "sha256": artifact_sha256,
        }
    ]
    assert len(store._read_publish_commit_ledger()) == 1


def test_committed_registry_crash_recovery_emits_receipt_before_manifest_cleanup(
    tmp_path,
):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(
        tmp_path / "factory-artifacts",
        clock=lambda: COMMITTED_AT,
    )
    artifact_sha256 = store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )
    transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_state),
        artifact_sha256=artifact_sha256,
    )
    transaction_path = factory_module._publish_transaction_path(registry)
    atomic_write_json(transaction_path, transaction)

    factory_module._recover_interrupted_factory_publish(registry, store)

    assert not transaction_path.exists()
    receipt = store.publication_receipt(
        "model",
        "model-v2",
        expected_sha256=artifact_sha256,
    )
    assert receipt["final_registry_sha256"] == factory_module._registry_state_sha256(
        registry._read()
    )
    assert receipt["committed_at"] == "2026-01-09T12:00:00Z"


def test_precommit_recovery_rolls_back_artifact_without_publish_receipt(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    original_state = registry._read()

    final_registry = ScientificRegistry.initialize_pristine(
        tmp_path / "final-registry.json"
    )
    final_registry.append(
        ResearchQuestion(
            "question-hypothetical-final",
            "This state must not become authoritative during precommit recovery.",
            SHA_A,
            T0,
        )
    )
    final_state = final_registry._read()

    store = FactoryArtifactStore(
        tmp_path / "factory-artifacts",
        clock=lambda: COMMITTED_AT,
    )
    artifact_sha256 = store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )
    transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_state),
        artifact_sha256=artifact_sha256,
    )
    transaction_path = factory_module._publish_transaction_path(registry)
    atomic_write_json(transaction_path, transaction)

    factory_module._recover_interrupted_factory_publish(registry, store)

    assert not transaction_path.exists()
    assert not store.path_for_testing("model", "model-v2").exists()
    assert not store._publish_commit_ledger_path().exists()
    assert factory_module._registry_state_sha256(registry._read()) == transaction[
        "original_registry_sha256"
    ]


def test_publish_receipt_rejects_artifact_rebinding_to_another_transaction(tmp_path):
    store = FactoryArtifactStore(
        tmp_path / "factory-artifacts",
        clock=lambda: COMMITTED_AT,
    )
    artifact_sha256 = store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )
    first = _transaction(
        original_sha256=SHA_A,
        final_sha256=SHA_B,
        artifact_sha256=artifact_sha256,
    )
    store._record_publish_commit(first)

    conflicting = _transaction(
        original_sha256=SHA_B,
        final_sha256="c" * 64,
        artifact_sha256=artifact_sha256,
    )
    with pytest.raises(
        ValueError,
        match="already bound to another publish commit",
    ):
        store._record_publish_commit(conflicting)
