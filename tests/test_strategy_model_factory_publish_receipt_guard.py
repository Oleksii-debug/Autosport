from datetime import datetime, timezone
import json

import pytest

import autosport.strategy_model_factory as factory_module
from autosport.integrity import atomic_write_json
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolated_publish_authority(tmp_path, monkeypatch):
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root))


def _transaction(
    *,
    original_sha256,
    final_sha256,
    artifact_sha256,
    identity="model-v2",
):
    return {
        "schema_version": 1,
        "phase": "prepared",
        "original_registry_sha256": original_sha256,
        "final_registry_sha256": final_sha256,
        "artifacts": [
            {
                "kind": "model",
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


def _write_artifact(store, identity="model-v2"):
    return store.write(
        "model",
        identity,
        {"schema_version": 1, "model_id": identity},
    )


def test_publish_commit_receipt_binds_artifact_and_final_registry(tmp_path):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = _write_artifact(store)
    transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_state),
        artifact_sha256=artifact_sha256,
    )
    atomic_write_json(factory_module._publish_transaction_path(registry), transaction)

    first = factory_module._record_committed_factory_publish(registry, store)
    second = factory_module._record_committed_factory_publish(registry, store)
    receipt = store.publication_receipt(
        "model",
        "model-v2",
        expected_sha256=artifact_sha256,
    )

    assert first == second == receipt
    assert receipt["final_registry_sha256"] == transaction["final_registry_sha256"]
    committed_at = datetime.fromisoformat(
        receipt["committed_at"].replace("Z", "+00:00")
    )
    assert committed_at.tzinfo is not None
    assert len(store._read_publish_commit_ledger()) == 1


def test_committed_registry_recovery_emits_receipt_before_manifest_cleanup(tmp_path):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = _write_artifact(store)
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


def test_precommit_recovery_rolls_back_without_publication_receipt(tmp_path):
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
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = _write_artifact(store)
    transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_registry._read()),
        artifact_sha256=artifact_sha256,
    )
    transaction_path = factory_module._publish_transaction_path(registry)
    atomic_write_json(transaction_path, transaction)

    factory_module._recover_interrupted_factory_publish(registry, store)

    assert not transaction_path.exists()
    assert not store.path_for_testing("model", "model-v2").exists()
    assert not store._publish_commit_ledger_path().exists()


def test_publish_commit_issuer_rejects_precommit_registry_state(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    original_state = registry._read()
    final_registry = ScientificRegistry.initialize_pristine(
        tmp_path / "final-registry.json"
    )
    final_registry.append(
        ResearchQuestion(
            "question-future-final",
            "Prepared is not committed.",
            SHA_A,
            T0,
        )
    )
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = _write_artifact(store)
    transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_registry._read()),
        artifact_sha256=artifact_sha256,
    )
    atomic_write_json(factory_module._publish_transaction_path(registry), transaction)

    with pytest.raises(
        ValueError,
        match="requires the durable final registry state",
    ):
        factory_module._record_committed_factory_publish(registry, store)

    assert not store._publish_commit_ledger_path().exists()


def test_materialization_clock_cannot_backdate_publish_commit(tmp_path):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    injected_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    store = FactoryArtifactStore(
        tmp_path / "factory-artifacts",
        clock=lambda: injected_past,
    )
    artifact_sha256 = _write_artifact(store)
    transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_state),
        artifact_sha256=artifact_sha256,
    )
    atomic_write_json(factory_module._publish_transaction_path(registry), transaction)
    before = datetime.now(timezone.utc)

    receipt = factory_module._record_committed_factory_publish(registry, store)

    committed_at = datetime.fromisoformat(
        receipt["committed_at"].replace("Z", "+00:00")
    )
    assert committed_at >= before
    assert committed_at > injected_past


def test_publish_commit_clock_is_not_caller_overrideable(tmp_path):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = _write_artifact(store)
    transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_state),
        artifact_sha256=artifact_sha256,
    )

    with pytest.raises(TypeError):
        store._append_publish_commit_record(
            transaction,
            _utc_now=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc),
        )


def test_deleted_or_recomputed_publish_ledger_is_rejected_by_machine_authority(
    tmp_path,
):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = _write_artifact(store)
    transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_state),
        artifact_sha256=artifact_sha256,
    )
    atomic_write_json(factory_module._publish_transaction_path(registry), transaction)
    factory_module._record_committed_factory_publish(registry, store)

    ledger_path = store._publish_commit_ledger_path()
    original_ledger = ledger_path.read_text(encoding="utf-8")
    ledger = json.loads(original_ledger)
    ledger["records"][0]["committed_at"] = "2000-01-01T00:00:00Z"
    ledger["records"][0]["record_sha256"] = store._publish_commit_digest(
        ledger["records"][0]
    )
    atomic_write_json(ledger_path, ledger)

    with pytest.raises(MonotonicAuthorityRollbackError):
        store.publication_receipt(
            "model",
            "model-v2",
            expected_sha256=artifact_sha256,
        )

    ledger_path.write_text(original_ledger, encoding="utf-8")
    store.publication_receipt(
        "model",
        "model-v2",
        expected_sha256=artifact_sha256,
    )
    ledger_path.unlink()

    with pytest.raises(MonotonicAuthorityRollbackError):
        store._read_publish_commit_ledger()


def test_older_complete_publish_ledger_snapshot_is_rejected(tmp_path):
    registry, original_state, first_final = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    transaction_path = factory_module._publish_transaction_path(registry)
    first_sha256 = _write_artifact(store)
    atomic_write_json(
        transaction_path,
        _transaction(
            original_sha256=factory_module._registry_state_sha256(original_state),
            final_sha256=factory_module._registry_state_sha256(first_final),
            artifact_sha256=first_sha256,
        ),
    )
    factory_module._record_committed_factory_publish(registry, store)
    ledger_path = store._publish_commit_ledger_path()
    first_ledger = ledger_path.read_text(encoding="utf-8")

    second_original = registry._read()
    registry.append(
        ResearchQuestion(
            "question-second-commit",
            "Can an older receipt ledger be restored after a newer publish?",
            SHA_B,
            T0,
        )
    )
    second_sha256 = _write_artifact(store, "model-v3")
    atomic_write_json(
        transaction_path,
        _transaction(
            original_sha256=factory_module._registry_state_sha256(second_original),
            final_sha256=factory_module._registry_state_sha256(registry._read()),
            artifact_sha256=second_sha256,
            identity="model-v3",
        ),
    )
    factory_module._record_committed_factory_publish(registry, store)
    assert len(store._read_publish_commit_ledger()) == 2

    ledger_path.write_text(first_ledger, encoding="utf-8")
    with pytest.raises(MonotonicAuthorityRollbackError):
        store.publication_receipt(
            "model",
            "model-v2",
            expected_sha256=first_sha256,
        )
