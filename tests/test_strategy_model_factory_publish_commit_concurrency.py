from __future__ import annotations

from threading import Event, Thread

import pytest

from autosport.monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
)
from autosport.strategy_model_factory import FactoryArtifactStore
from autosport.workspace_lock import WorkspaceEconomicLockBusyError


def test_publish_receipt_reader_cannot_abort_live_writer_prepare(
    tmp_path,
    monkeypatch,
):
    """Reader contention must fail closed until one publish transition commits."""

    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    artifact_sha256 = store.write(
        "model",
        "model-v2",
        {"schema_version": 1, "model_id": "model-v2"},
    )
    transaction = {
        "schema_version": 1,
        "phase": "prepared",
        "original_registry_sha256": "a" * 64,
        "final_registry_sha256": "b" * 64,
        "artifacts": [
            {
                "kind": "model",
                "identity": "model-v2",
                "sha256": artifact_sha256,
            }
        ],
    }

    durable_prepare_reached = Event()
    allow_writer_to_continue = Event()
    canonical_prepare = MonotonicWorkspaceAuthority.prepare

    def pause_after_durable_prepare(self, *args, **kwargs):
        prepared = canonical_prepare(self, *args, **kwargs)
        durable_prepare_reached.set()
        if not allow_writer_to_continue.wait(timeout=10):
            raise AssertionError("test did not release prepared publication writer")
        return prepared

    monkeypatch.setattr(
        MonotonicWorkspaceAuthority,
        "prepare",
        pause_after_durable_prepare,
    )

    writer_results: list[dict[str, object]] = []
    writer_errors: list[BaseException] = []

    def publish() -> None:
        try:
            writer_results.append(store._append_publish_commit_record(transaction))
        except BaseException as exc:
            writer_errors.append(exc)

    writer = Thread(target=publish, name="factory-publish-writer", daemon=True)
    writer.start()
    assert durable_prepare_reached.wait(timeout=10)

    try:
        with pytest.raises(WorkspaceEconomicLockBusyError):
            store.publication_receipt(
                "model",
                "model-v2",
                expected_sha256=artifact_sha256,
            )
    finally:
        allow_writer_to_continue.set()

    writer.join(timeout=10)
    assert not writer.is_alive()
    assert writer_errors == []
    assert len(writer_results) == 1

    receipt = store.publication_receipt(
        "model",
        "model-v2",
        expected_sha256=artifact_sha256,
    )
    assert receipt == writer_results[0]
    assert store._read_publish_commit_ledger() == [receipt]

    history = store._publish_commit_authority().read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
