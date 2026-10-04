from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autosport.ingestion_health import SourceHealthStore
from autosport.monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRollbackError,
)


T0 = "2026-10-04T00:00:00Z"
T1 = "2026-10-04T00:01:00Z"


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "workspace") -> SourceHealthStore:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )
    return SourceHealthStore(tmp_path / name / "source-health.json")


def _record_success(store: SourceHealthStore) -> None:
    state = store.record_success(
        "provider-a",
        now=T0,
        received=1,
        accepted=1,
        rejected=0,
        cursor="cursor-1",
        latest_source_ts=T0,
        quality_flags=(),
    )
    assert state.status == "healthy"


def _record_provider_failure(store: SourceHealthStore) -> None:
    state = store.record_failure(
        "provider-a",
        now=T1,
        error=RuntimeError("provider unavailable"),
        failure_kind="provider_unavailable",
    )
    assert state.status == "failed"
    assert state.last_failure_kind == "provider_unavailable"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_valid_old_source_health_image_is_rejected_after_newer_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    _record_success(store)
    valid_old = store.path.read_bytes()

    _record_provider_failure(store)
    current = store.path.read_bytes()
    assert current != valid_old

    # Both the projection and causal history are internally valid at T0. Restoring
    # those bytes must not erase the newer provider-unavailable/backoff evidence.
    store.path.write_bytes(valid_old)

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority|match",
    ):
        SourceHealthStore(store.path)

    assert store.path.read_bytes() == valid_old


def test_deleting_source_health_after_committed_history_cannot_rebootstrap_pristine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    _record_success(store)
    _record_provider_failure(store)

    store.path.unlink()

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority|match",
    ):
        SourceHealthStore(store.path)

    assert not store.path.exists()
    assert not store.path.with_suffix(store.path.suffix + ".tmp").exists()


def test_prepare_without_local_publish_is_aborted_on_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    _record_success(store)

    observed = _sha256(store.path.read_bytes())
    intended = "f" * 64
    authority = store._monotonic_authority()
    binding = store._authority_binding(observed, intended, kind="PUBLISH")
    authority.prepare(
        tx_id="test-pre-publish-crash",
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    reopened = SourceHealthStore(store.path)
    assert reopened.get("provider-a").status == "healthy"
    history = reopened._monotonic_authority().read_history()
    assert history[-1].phase is AuthorityPhase.ABORT
    assert history[-1].previous_committed_state_sha256 == observed


def test_published_bytes_without_commit_are_recovered_on_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "target")
    _record_success(store)
    observed_bytes = store.path.read_bytes()
    observed = _sha256(observed_bytes)

    # Generate a second, independently valid SourceHealthStore image with the exact
    # same prefix and one newer typed provider-unavailable transition. Authority
    # metadata is external and therefore does not alter the store bytes.
    future = _store(tmp_path, monkeypatch, "future")
    _record_success(future)
    _record_provider_failure(future)
    intended_bytes = future.path.read_bytes()
    intended = _sha256(intended_bytes)
    assert intended_bytes != observed_bytes

    authority = store._monotonic_authority()
    binding = store._authority_binding(observed, intended, kind="PUBLISH")
    authority.prepare(
        tx_id="test-post-publish-crash",
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    store.path.write_bytes(intended_bytes)

    reopened = SourceHealthStore(store.path)
    state = reopened.get("provider-a")
    assert state.status == "failed"
    assert state.last_failure_kind == "provider_unavailable"

    history = reopened._monotonic_authority().read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].intended_state_sha256 == intended
