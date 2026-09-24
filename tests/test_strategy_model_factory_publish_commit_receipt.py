from datetime import datetime, timezone
import json

import pytest

import autosport.strategy_model_factory as factory_module
from autosport.integrity import atomic_write_json
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-01-01T00:00:00+00:00"
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
    registry, original_state, final_state = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
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

    first = factory_module._record_committed_factory_publish(registry, store)
    second = factory_module._record_committed_factory_publish(registry, store)
    receipt = store.publication_receipt(
        "model",
        "model-v2",
        expected_sha256=artifact_sha256,
    )

    assert first == second == receipt
    committed_at = datetime.fromisoformat(
        receipt["committed_at"].replace("Z", "+00:00")
    )
    assert committed_at.tzinfo is not None
    assert receipt["original_registry_sha256"] == transaction[
        "original_registry_sha256"
    ]
    assert receipt["final_registry_sha256"] == transaction["final_registry_sha256"]
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
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
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
    committed_at = datetime.fromisoformat(
        receipt["committed_at"].replace("Z", "+00:00")
    )
    assert committed_at.tzinfo is not None


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

    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
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


def test_injected_materialization_clock_cannot_backdate_publish_commit(tmp_path):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    injected_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    store = FactoryArtifactStore(
        tmp_path / "factory-artifacts",
        clock=lambda: injected_past,
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
    atomic_write_json(factory_module._publish_transaction_path(registry), transaction)
    before = datetime.now(timezone.utc)

    receipt = factory_module._record_committed_factory_publish(registry, store)

    committed_at = datetime.fromisoformat(
        receipt["committed_at"].replace("Z", "+00:00")
    )
    assert committed_at >= before
    assert committed_at > injected_past


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
            "A prepared transaction alone is not a committed factory publication.",
            SHA_A,
            T0,
        )
    )
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )
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

def test_publish_commit_clock_cannot_be_retargeted_by_module_datetime_rebind(
    tmp_path,
    monkeypatch,
):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
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
    atomic_write_json(factory_module._publish_transaction_path(registry), transaction)
    before = datetime.now(timezone.utc)

    class BackdatedDatetime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2000, 1, 1, tzinfo=timezone.utc)

    class ReboundTimezone:
        utc = timezone.utc

    monkeypatch.setattr(factory_module, "datetime", BackdatedDatetime)
    monkeypatch.setattr(factory_module, "timezone", ReboundTimezone)

    receipt = factory_module._record_committed_factory_publish(registry, store)

    committed_at = datetime.fromisoformat(
        receipt["committed_at"].replace("Z", "+00:00")
    )
    assert committed_at >= before
    assert committed_at.year != 2000


def test_publish_commit_rejects_wall_clock_rollback_after_prior_receipt(tmp_path):
    registry, original_state, final_state = _final_registry_state(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    first_sha256 = store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )
    first_transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_state),
        artifact_sha256=first_sha256,
    )
    transaction_path = factory_module._publish_transaction_path(registry)
    atomic_write_json(transaction_path, first_transaction)
    factory_module._record_committed_factory_publish(registry, store)

    ledger_path = store._publish_commit_ledger_path()
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    first_record = ledger["records"][0]
    first_record["committed_at"] = "2999-01-01T00:00:00Z"
    first_record["record_sha256"] = store._publish_commit_digest(first_record)
    atomic_write_json(ledger_path, ledger)

    second_original_state = registry._read()
    registry.append(
        ResearchQuestion(
            "question-second-commit",
            "Does publication chronology remain monotone after clock rollback?",
            SHA_B,
            T0,
        )
    )
    second_final_state = registry._read()
    second_sha256 = store.write(
        "model",
        "model-v3",
        {"schema_version": 1, "model_id": "model-v3"},
    )
    second_transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(
            second_original_state
        ),
        final_sha256=factory_module._registry_state_sha256(second_final_state),
        artifact_sha256=second_sha256,
        identity="model-v3",
    )
    atomic_write_json(transaction_path, second_transaction)

    with pytest.raises(
        ValueError,
        match="factory publish committed_at moved backwards",
    ):
        factory_module._record_committed_factory_publish(registry, store)

    assert len(store._read_publish_commit_ledger()) == 1
    with pytest.raises(
        ValueError,
        match="factory publication receipt is missing or ambiguous",
    ):
        store.publication_receipt(
            "model",
            "model-v3",
            expected_sha256=second_sha256,
        )

