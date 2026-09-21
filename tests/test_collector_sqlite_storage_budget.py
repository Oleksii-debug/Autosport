from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    CollectorStorageBackpressureError,
    CollectorStorageBudgetError,
)


_INSTANT = "2026-09-21T00:00:00+00:00"


def _delta(position: int, *, padding: int = 0) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=f"delta-{position}",
        source_id="budget-source",
        lawful_terms_ref="terms" + ("x" * padding),
        retention_ref="retention-v1",
        stream_epoch="epoch-1",
        source_cursor=f"cursor-{position}",
        cursor_position=position,
        event_dedupe_key=f"event-key-{position}",
        event_id=f"event-{position}",
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


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "4096"])
def test_rejects_invalid_byte_budget(tmp_path: Path, value: object) -> None:
    with pytest.raises(CollectorStorageBudgetError):
        CollectorDeltaStore(tmp_path / "collector.sqlite", max_bytes=value)  # type: ignore[arg-type]


def test_rejects_budget_smaller_than_one_sqlite_page(tmp_path: Path) -> None:
    with pytest.raises(CollectorStorageBudgetError, match="smaller than one SQLite page"):
        CollectorDeltaStore(tmp_path / "collector.sqlite", max_bytes=1)


def test_reopen_fails_closed_when_existing_store_exceeds_budget(tmp_path: Path) -> None:
    path = tmp_path / "collector.sqlite"
    store = CollectorDeltaStore(path)
    assert store.append(_delta(1, padding=16_384)) is True
    page_size, page_count = _page_geometry(path)
    assert page_count > 1

    with pytest.raises(CollectorStorageBudgetError, match="exceeds configured max_bytes"):
        CollectorDeltaStore(path, max_bytes=(page_count - 1) * page_size)

    # Failing the smaller-budget reopen cannot damage the pre-existing authority.
    reopened = CollectorDeltaStore(path)
    assert reopened.get("delta-1") == _delta(1, padding=16_384)


def test_page_ceiling_rejects_growth_and_rolls_back_canonical_state(tmp_path: Path) -> None:
    path = tmp_path / "collector.sqlite"
    initial = CollectorDeltaStore(path)
    first = _delta(1)
    assert initial.append(first) is True
    page_size, page_count = _page_geometry(path)
    exact_current_budget = page_size * page_count

    bounded = CollectorDeltaStore(path, max_bytes=exact_current_budget)
    assert bounded.configured_max_bytes == exact_current_budget
    before_checkpoint = bounded.stream_checkpoint("budget-source", "epoch-1")
    before_size = path.stat().st_size

    connection = bounded._connect()
    try:
        assert int(connection.execute("PRAGMA max_page_count").fetchone()[0]) == page_count
    finally:
        connection.close()

    # Idempotent duplicate admission remains possible at capacity because it does
    # not allocate a new canonical page.
    assert bounded.append(first) is False

    second = _delta(2, padding=page_size * 16)
    with pytest.raises(CollectorStorageBackpressureError, match="RETENTION_REQUIRED"):
        bounded.append(second)

    assert bounded.get("delta-2") is None
    assert bounded.get("delta-1") == first
    assert bounded.stream_checkpoint("budget-source", "epoch-1") == before_checkpoint
    assert path.stat().st_size == before_size
    assert path.stat().st_size <= exact_current_budget

    # The rejected transaction leaves the canonical file readable after restart.
    reopened = CollectorDeltaStore(path, max_bytes=exact_current_budget)
    assert reopened.get("delta-1") == first
    assert reopened.get("delta-2") is None


def test_durable_budget_applies_to_second_unconfigured_canonical_handle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.sqlite"
    initial = CollectorDeltaStore(path)
    first = _delta(1)
    assert initial.append(first) is True
    page_size, page_count = _page_geometry(path)
    exact_current_budget = page_size * page_count

    bounded = CollectorDeltaStore(path, max_bytes=exact_current_budget)
    assert bounded.configured_max_bytes == exact_current_budget
    before_size = path.stat().st_size

    # The budget belongs to the canonical database path, not to one Python object.
    # A second exact canonical handle created without max_bytes must resolve the
    # durable authority and may not silently reopen an unbounded write path.
    other = CollectorDeltaStore(path)
    assert other.configured_max_bytes == exact_current_budget
    other_connection = other._connect()
    try:
        assert (
            int(other_connection.execute("PRAGMA max_page_count").fetchone()[0])
            == page_count
        )
    finally:
        other_connection.close()

    second = _delta(2, padding=page_size * 16)
    with pytest.raises(CollectorStorageBackpressureError, match="RETENTION_REQUIRED"):
        other.append(second)

    assert other.get("delta-2") is None
    assert bounded.get("delta-2") is None
    assert path.stat().st_size == before_size
    assert path.stat().st_size <= exact_current_budget


def test_conflicting_second_handle_cannot_widen_or_narrow_durable_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.sqlite"
    store = CollectorDeltaStore(path)
    assert store.append(_delta(1)) is True
    page_size, page_count = _page_geometry(path)
    exact_current_budget = page_size * page_count

    canonical = CollectorDeltaStore(path, max_bytes=exact_current_budget)
    assert canonical.configured_max_bytes == exact_current_budget

    with pytest.raises(CollectorStorageBudgetError, match="conflicts with durable"):
        CollectorDeltaStore(path, max_bytes=exact_current_budget + page_size)

    smaller = exact_current_budget - page_size
    if smaller > 0:
        with pytest.raises(CollectorStorageBudgetError, match="conflicts with durable"):
            CollectorDeltaStore(path, max_bytes=smaller)

    # Exact replay is allowed and an unconfigured reopen inherits the same budget.
    same = CollectorDeltaStore(path, max_bytes=exact_current_budget)
    inherited = CollectorDeltaStore(path)
    assert same.configured_max_bytes == exact_current_budget
    assert inherited.configured_max_bytes == exact_current_budget


def test_unconfigured_store_preserves_existing_growth_behavior(tmp_path: Path) -> None:
    path = tmp_path / "collector.sqlite"
    store = CollectorDeltaStore(path)
    assert store.configured_max_bytes is None
    assert store.append(_delta(1)) is True
    page_size, before_pages = _page_geometry(path)

    assert store.append(_delta(2, padding=page_size * 16)) is True
    _, after_pages = _page_geometry(path)
    assert after_pages > before_pages
    assert store.get("delta-2") == _delta(2, padding=page_size * 16)
