from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from autosport import collector_sqlite_bounded_storage as budget_module
from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    CollectorStorageBudgetError,
)


_INSTANT = "2026-09-21T00:00:00+00:00"


def _delta(position: int, *, padding: int = 0) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=f"race-delta-{position}",
        source_id="budget-race-source",
        lawful_terms_ref="terms" + ("x" * padding),
        retention_ref="retention-v1",
        stream_epoch="epoch-1",
        source_cursor=f"cursor-{position}",
        cursor_position=position,
        event_dedupe_key=f"race-event-key-{position}",
        event_id=f"race-event-{position}",
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


def test_first_budget_publication_revalidates_geometry_under_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "collector.sqlite"
    initial = CollectorDeltaStore(path)
    assert initial.append(_delta(1)) is True
    page_size, page_count = _page_geometry(path)
    requested_budget = page_size * page_count

    # Writer A must already own BEGIN IMMEDIATE before publisher B starts. Pausing
    # the instance append hook does that without sleeps because the canonical append
    # acquires the writer transaction before dispatching to _append_connection.
    writer = CollectorDeltaStore(path)
    writer_locked = threading.Event()
    release_writer = threading.Event()
    original_append_connection = writer._append_connection

    def paused_append_connection(connection: sqlite3.Connection, delta: CollectorDelta) -> bool:
        writer_locked.set()
        if not release_writer.wait(timeout=10):
            raise AssertionError("timed out waiting to release lock-first writer")
        return original_append_connection(connection, delta)

    writer._append_connection = paused_append_connection  # type: ignore[method-assign]
    writer_outcome: dict[str, BaseException | bool] = {}

    def append_while_holding_writer_lock() -> None:
        try:
            writer_outcome["result"] = writer.append(
                _delta(2, padding=page_size * 16)
            )
        except BaseException as exc:
            writer_outcome["error"] = exc

    writer_thread = threading.Thread(target=append_while_holding_writer_lock)
    writer_thread.start()
    assert writer_locked.wait(timeout=10)

    # Drive the publication helper directly so the signal is exactly its pre-lock
    # page-ceiling application. B then blocks in BEGIN IMMEDIATE behind writer A.
    publisher_prelocked = threading.Event()
    original_apply_page_budget = budget_module._apply_page_budget

    def signaled_apply_page_budget(connection: sqlite3.Connection, budget: int) -> None:
        original_apply_page_budget(connection, budget)
        publisher_prelocked.set()

    monkeypatch.setattr(budget_module, "_apply_page_budget", signaled_apply_page_budget)
    publisher_outcome: dict[str, BaseException | int | None] = {}

    def publish_budget() -> None:
        connection = sqlite3.connect(path, timeout=10)
        try:
            publisher_outcome["result"] = budget_module._resolve_effective_budget(
                connection,
                requested_budget,
            )
        except BaseException as exc:
            publisher_outcome["error"] = exc
        finally:
            connection.close()

    publisher_thread = threading.Thread(target=publish_budget)
    publisher_thread.start()
    assert publisher_prelocked.wait(timeout=10)

    release_writer.set()
    writer_thread.join(timeout=10)
    publisher_thread.join(timeout=10)
    assert not writer_thread.is_alive()
    assert not publisher_thread.is_alive()

    # A legitimately won the writer lock before any durable ceiling existed and can
    # grow. B must therefore reject the now-stale smaller request under its newly
    # acquired writer lock, rather than commit contradictory durable metadata.
    assert writer_outcome == {"result": True}
    assert isinstance(publisher_outcome.get("error"), CollectorStorageBudgetError)
    assert "exceeds configured max_bytes" in str(publisher_outcome["error"])
    _, after_pages = _page_geometry(path)
    assert after_pages > page_count

    check = sqlite3.connect(path)
    try:
        row = check.execute(
            "SELECT value FROM collector_meta WHERE key=?",
            (budget_module._BUDGET_META_KEY,),
        ).fetchone()
    finally:
        check.close()
    assert row is None

    # The pre-budget append remains canonical and readable, while trying to bind the
    # obsolete smaller budget still fails closed on a clean reopen.
    reopened = CollectorDeltaStore(path)
    assert reopened.configured_max_bytes is None
    assert reopened.get("race-delta-2") == _delta(2, padding=page_size * 16)
    with pytest.raises(CollectorStorageBudgetError, match="exceeds configured max_bytes"):
        CollectorDeltaStore(path, max_bytes=requested_budget)
