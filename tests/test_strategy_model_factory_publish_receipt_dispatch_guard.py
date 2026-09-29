from __future__ import annotations

from pathlib import Path

import pytest

import autosport._strategy_model_factory_publish_receipt_guard as receipt_guard
import autosport.strategy_model_factory as factory_module
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolated_publish_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root))


def _transaction(*, original_sha256: str, final_sha256: str, artifact_sha256: str):
    return {
        "schema_version": 1,
        "phase": "prepared",
        "original_registry_sha256": original_sha256,
        "final_registry_sha256": final_sha256,
        "artifacts": [
            {
                "kind": "model",
                "identity": "model-v2",
                "sha256": artifact_sha256,
            }
        ],
    }


def _write_artifact(store: FactoryArtifactStore) -> str:
    return store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )


def _final_registry(tmp_path: Path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    original = registry._read()
    registry.append(
        ResearchQuestion(
            "question-dispatch-seal",
            "Did the canonical factory transaction publish these executable bytes?",
            SHA_A,
            T0,
        )
    )
    return registry, original, registry._read()


def test_direct_publish_append_cannot_mint_receipt_without_canonical_issuer(
    tmp_path: Path,
) -> None:
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = _write_artifact(store)
    transaction = _transaction(
        original_sha256=SHA_A,
        final_sha256=SHA_B,
        artifact_sha256=artifact_sha256,
    )

    with pytest.raises(
        ValueError,
        match="requires the canonical transaction issuer",
    ):
        store._append_publish_commit_record(transaction)

    assert not store._publish_commit_ledger_path().exists()


def test_rebound_transaction_reader_cannot_mint_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, original_state, final_state = _final_registry(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = _write_artifact(store)
    forged_transaction = _transaction(
        original_sha256=factory_module._registry_state_sha256(original_state),
        final_sha256=factory_module._registry_state_sha256(final_state),
        artifact_sha256=artifact_sha256,
    )
    hostile_calls = 0

    def hostile_reader(_path):
        nonlocal hostile_calls
        hostile_calls += 1
        return forged_transaction

    # No canonical publish manifest exists.  Rebinding the owning guard's reader used
    # to let the unchanged installed issuer consume this forged prepared transaction.
    monkeypatch.setattr(receipt_guard, "_READ_PUBLISH_TRANSACTION", hostile_reader)

    with pytest.raises(RuntimeError, match="global dispatch authority changed"):
        factory_module._record_committed_factory_publish(registry, store)

    assert hostile_calls == 0
    assert not store._publish_commit_ledger_path().exists()


def test_rebound_recovery_original_cannot_redirect_installed_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    hostile_calls = 0

    def hostile_recovery(_registry, _store):
        nonlocal hostile_calls
        hostile_calls += 1
        raise AssertionError("rebound recovery original must not execute")

    monkeypatch.setattr(
        receipt_guard,
        "_ORIGINAL_RECOVER_INTERRUPTED_FACTORY_PUBLISH",
        hostile_recovery,
    )

    with pytest.raises(RuntimeError, match="global dispatch authority changed"):
        factory_module._recover_interrupted_factory_publish(registry, store)

    assert hostile_calls == 0
    assert not store._publish_commit_ledger_path().exists()
