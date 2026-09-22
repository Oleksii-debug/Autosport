from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    CollectorStorageBackpressureError,
    CursorRegressionError,
)
from autosport.collector_service import CollectorServiceConfig, HeadlessCollectorService
from autosport.event_lifecycle import (
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EventPhase,
)


_INSTANT = "2026-09-22T18:00:00+00:00"


def _delta(
    position: int,
    *,
    delta_id: str | None = None,
    source_id: str = "source-x",
    stream_epoch: str = "epoch-1",
    padding: int = 0,
) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id or f"delta-{position}",
        source_id=source_id,
        lawful_terms_ref="terms-v1" + ("x" * padding),
        retention_ref="retention-v1",
        stream_epoch=stream_epoch,
        source_cursor=f"cursor-{position}",
        cursor_position=position,
        event_dedupe_key=f"event-key-{position}",
        event_id="source-x:event-1",
        source_payload_digest="a" * 64,
        canonical_event_digest="b" * 64,
        source_observed_at=_INSTANT,
        collector_received_at=_INSTANT,
        collector_committed_at=_INSTANT,
        desktop_available_at=_INSTANT,
    )


def _page_geometry(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        return page_size, page_count
    finally:
        connection.close()


class _Source:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(self, deltas: tuple[CollectorDelta, ...]) -> None:
        self._deltas = deltas

    def fetch_catalog_page(self, checkpoint: object) -> CatalogPage:
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-epoch-1",
            cursor="catalog-1",
            position=1,
            events=(
                CatalogEvent(
                    source_id=self.source_id,
                    sport="table_tennis",
                    event_id="source-x:event-1",
                    phase=EventPhase.PRE_MATCH,
                    available_at=_INSTANT,
                ),
            ),
        )

    def fetch_deltas(
        self,
        checkpoint: object,
        records: object,
        max_items: int,
    ) -> tuple[CollectorDelta, ...]:
        assert len(self._deltas) <= max_items
        return self._deltas


def test_runtime_batch_uses_one_sqlite_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollectorDeltaStore(tmp_path / "collector.sqlite")
    original_connect = store._connect
    connect_calls = 0

    def counted_connect() -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        return original_connect()

    monkeypatch.setattr(store, "_connect", counted_connect)

    changed = store._append_batch_with_runtime_stream_epoch(
        (_delta(1), _delta(2)),
        activated_at=_INSTANT,
    )

    assert changed == (True, True)
    assert connect_calls == 1


def test_runtime_batch_rolls_back_every_delta_on_late_causal_failure(
    tmp_path: Path,
) -> None:
    store = CollectorDeltaStore(tmp_path / "collector.sqlite")
    first = _delta(1, delta_id="first")
    equal_cursor_without_revision = _delta(1, delta_id="invalid-second")

    with pytest.raises(CursorRegressionError):
        store._append_batch_with_runtime_stream_epoch(
            (first, equal_cursor_without_revision),
            activated_at=_INSTANT,
        )

    assert store.get(first.delta_id) is None
    assert store.get(equal_cursor_without_revision.delta_id) is None
    assert store.stream_checkpoint("source-x", "epoch-1") is None


def test_runtime_batch_rolls_back_every_delta_on_precommit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollectorDeltaStore(tmp_path / "collector.sqlite")

    def fail_before_commit(raw: dict[str, object]) -> None:
        raise RuntimeError("injected precommit failure")

    monkeypatch.setattr(store, "_write", fail_before_commit)

    with pytest.raises(RuntimeError, match="injected precommit failure"):
        store._append_batch_with_runtime_stream_epoch(
            (_delta(1), _delta(2)),
            activated_at=_INSTANT,
        )

    assert store.get("delta-1") is None
    assert store.get("delta-2") is None
    assert store.stream_checkpoint("source-x", "epoch-1") is None


def test_runtime_batch_duplicate_accounting_is_stable_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.sqlite"
    first = _delta(1)
    second = _delta(2)
    store = CollectorDeltaStore(path)

    assert store._append_batch_with_runtime_stream_epoch(
        (first, second),
        activated_at=_INSTANT,
    ) == (True, True)
    assert store._append_batch_with_runtime_stream_epoch(
        (first, second),
        activated_at=_INSTANT,
    ) == (False, False)

    reopened = CollectorDeltaStore(path)
    assert reopened.get(first.delta_id) == first
    assert reopened.get(second.delta_id) == second
    checkpoint = reopened.stream_checkpoint("source-x", "epoch-1")
    assert checkpoint is not None
    assert checkpoint.last_delta_id == second.delta_id
    assert checkpoint.last_position == 2


def test_runtime_batch_rejects_mixed_source_before_any_write(
    tmp_path: Path,
) -> None:
    store = CollectorDeltaStore(tmp_path / "collector.sqlite")
    first = _delta(1)
    other_source = replace(_delta(2), source_id="source-y")

    with pytest.raises(
        ValueError,
        match="one source_id and stream_epoch",
    ):
        store._append_batch_with_runtime_stream_epoch(
            (first, other_source),
            activated_at=_INSTANT,
        )

    assert store.get(first.delta_id) is None
    assert store.get(other_source.delta_id) is None


def test_runtime_batch_storage_full_is_fail_closed_and_recoverable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.sqlite"
    seed = _delta(1)
    initial = CollectorDeltaStore(path)
    assert initial.append(seed) is True
    page_size, page_count = _page_geometry(path)
    max_bytes = page_size * page_count

    bounded = CollectorDeltaStore(path, max_bytes=max_bytes)
    oversized = _delta(2, padding=page_size * 32)

    with pytest.raises(
        CollectorStorageBackpressureError,
        match="RETENTION_REQUIRED",
    ):
        bounded._append_batch_with_runtime_stream_epoch(
            (seed, oversized),
            activated_at=_INSTANT,
        )

    assert bounded.get(seed.delta_id) == seed
    assert bounded.get(oversized.delta_id) is None
    reopened = CollectorDeltaStore(path, max_bytes=max_bytes)
    assert reopened.get(seed.delta_id) == seed
    assert reopened.get(oversized.delta_id) is None


def test_service_submits_provider_tuple_through_one_atomic_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollectorDeltaStore(tmp_path / "collector.sqlite")
    source = _Source((_delta(1), _delta(2)))
    service = HeadlessCollectorService(
        delta_store=store,
        lifecycle=ContinuousEventLifecycle(tmp_path / "catalog.json"),
        source=source,
        state_path=tmp_path / "service-state.json",
        run_id="atomic-batch-test",
        config=CollectorServiceConfig(
            max_items=10,
            poll_interval_seconds=1.0,
        ),
        clock=lambda: _INSTANT,
        sleep=lambda _seconds: None,
    )

    original_batch = store._append_batch_with_runtime_stream_epoch
    observed_batches: list[tuple[str, ...]] = []

    def tracked_batch(
        deltas: tuple[CollectorDelta, ...],
        *,
        activated_at: str,
    ) -> tuple[bool, ...]:
        observed_batches.append(tuple(delta.delta_id for delta in deltas))
        return original_batch(deltas, activated_at=activated_at)

    monkeypatch.setattr(
        store,
        "_append_batch_with_runtime_stream_epoch",
        tracked_batch,
    )

    result = service.run_cycle()

    assert observed_batches == [("delta-1", "delta-2")]
    assert result.committed_delta_ids == ("delta-1", "delta-2")
    assert result.duplicate_delta_ids == ()
    assert store.get("delta-1") == _delta(1)
    assert store.get("delta-2") == _delta(2)
