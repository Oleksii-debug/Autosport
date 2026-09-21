from __future__ import annotations

import json
import sqlite3
import threading
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


def _default_page_size() -> int:
    connection = sqlite3.connect(":memory:")
    try:
        return int(connection.execute("PRAGMA page_size").fetchone()[0])
    finally:
        connection.close()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "4096"])
def test_rejects_invalid_byte_budget(tmp_path: Path, value: object) -> None:
    with pytest.raises(CollectorStorageBudgetError):
        CollectorDeltaStore(tmp_path / "collector.sqlite", max_bytes=value)  # type: ignore[arg-type]


def test_rejects_budget_smaller_than_one_sqlite_page(tmp_path: Path) -> None:
    with pytest.raises(CollectorStorageBudgetError, match="smaller than one SQLite page"):
        CollectorDeltaStore(tmp_path / "collector.sqlite", max_bytes=1)


def test_new_store_schema_allocation_is_bounded_before_canonical_publish(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.sqlite"
    one_page_budget = _default_page_size()

    # One page is enough to pass budget geometry validation but not enough for the
    # canonical schema. The native ceiling must therefore fail during initialization
    # without publishing a partial canonical SQLite authority.
    with pytest.raises(CollectorStorageBudgetError, match="cannot initialize"):
        CollectorDeltaStore(path, max_bytes=one_page_budget)

    assert not path.exists()
    assert not Path(f"{path}-journal").exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_failed_bounded_legacy_migration_preserves_original_json_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.json"
    legacy_bytes = b'{"schema_version":1,"deltas":[],"streams":{}}'
    path.write_bytes(legacy_bytes)
    one_page_budget = _default_page_size()

    # Candidate construction/replay is allocation-bounded before the atomic switch.
    # A too-small native ceiling must leave the exact legacy authority untouched.
    with pytest.raises(CollectorStorageBudgetError, match="cannot initialize"):
        CollectorDeltaStore(path, max_bytes=one_page_budget)

    assert path.read_bytes() == legacy_bytes
    assert not path.with_name(f"{path.name}.legacy-v1.json").exists()
    assert not list(tmp_path.glob(f".{path.name}.sqlite-migrate-*.tmp"))


def test_failed_bounded_legacy_replay_overflow_preserves_nonempty_json_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.json"
    page_size = _default_page_size()
    max_bytes = page_size * 64

    # Prove this exact ceiling can admit a complete empty canonical store. The
    # migration below must therefore fail during legacy replay, not during schema
    # construction as the smaller-budget regression above already covers.
    probe = tmp_path / "empty-probe.sqlite"
    empty = CollectorDeltaStore(probe, max_bytes=max_bytes)
    assert empty.configured_max_bytes == max_bytes
    _, empty_pages = _page_geometry(probe)
    assert empty_pages <= max_bytes // page_size

    delta = _delta(1, padding=max_bytes * 4)
    legacy = {
        "schema_version": 1,
        "deltas": [delta.to_dict()],
        "streams": {
            "budget-source|epoch-1": {
                "source_id": "budget-source",
                "stream_epoch": "epoch-1",
                "last_cursor": "cursor-1",
                "last_position": 1,
                "last_delta_id": "delta-1",
            }
        },
    }
    legacy_bytes = json.dumps(
        legacy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    path.write_bytes(legacy_bytes)

    with pytest.raises(CollectorStorageBudgetError, match="cannot initialize"):
        CollectorDeltaStore(path, max_bytes=max_bytes)

    # Replay overflow is pre-switch: exact legacy bytes remain authoritative and
    # independently readable, with no backup or migration candidate published.
    assert path.read_bytes() == legacy_bytes
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert CollectorDelta.from_dict(decoded["deltas"][0]) == delta
    assert decoded["streams"] == legacy["streams"]
    assert not path.with_name(f"{path.name}.legacy-v1.json").exists()
    assert not list(tmp_path.glob(f".{path.name}.sqlite-migrate-*.tmp"))


def test_new_store_binds_budget_before_first_reopen(tmp_path: Path) -> None:
    path = tmp_path / "collector.sqlite"
    max_bytes = 1024 * 1024

    created = CollectorDeltaStore(path, max_bytes=max_bytes)
    assert created.configured_max_bytes == max_bytes

    inherited = CollectorDeltaStore(path)
    assert inherited.configured_max_bytes == max_bytes
    connection = inherited._connect()
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        expected_pages = max_bytes // page_size
        assert int(connection.execute("PRAGMA max_page_count").fetchone()[0]) == expected_pages
    finally:
        connection.close()


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
    durable_budget = exact_current_budget + (2 * page_size)

    canonical = CollectorDeltaStore(path, max_bytes=durable_budget)
    assert canonical.configured_max_bytes == durable_budget

    with pytest.raises(CollectorStorageBudgetError, match="conflicts with durable"):
        CollectorDeltaStore(path, max_bytes=durable_budget + page_size)

    with pytest.raises(CollectorStorageBudgetError, match="conflicts with durable"):
        CollectorDeltaStore(path, max_bytes=durable_budget - page_size)

    # Exact replay is allowed and an unconfigured reopen inherits the same budget.
    same = CollectorDeltaStore(path, max_bytes=durable_budget)
    inherited = CollectorDeltaStore(path)
    assert same.configured_max_bytes == durable_budget
    assert inherited.configured_max_bytes == durable_budget


def test_first_budget_activation_rebinds_already_open_writer_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.sqlite"
    initial = CollectorDeltaStore(path)
    first = _delta(1)
    assert initial.append(first) is True
    page_size, page_count = _page_geometry(path)
    exact_current_budget = page_size * page_count
    before_size = path.stat().st_size

    writer = CollectorDeltaStore(path)
    opened = threading.Event()
    resume = threading.Event()
    original_connect = writer._connect

    def paused_connect() -> sqlite3.Connection:
        connection = original_connect()
        opened.set()
        if not resume.wait(timeout=10):
            connection.close()
            raise AssertionError("timed out waiting to resume stale writer")
        return connection

    writer._connect = paused_connect  # type: ignore[method-assign]
    outcome: dict[str, BaseException | bool] = {}

    def append_from_stale_connection() -> None:
        try:
            outcome["result"] = writer.append(_delta(2, padding=page_size * 16))
        except BaseException as exc:  # capture thread outcome for deterministic assertion
            outcome["error"] = exc

    thread = threading.Thread(target=append_from_stale_connection)
    thread.start()
    assert opened.wait(timeout=10)

    # Publish the first durable budget after writer A has already opened an
    # unbounded connection but before it acquires BEGIN IMMEDIATE.
    bounded = CollectorDeltaStore(path, max_bytes=exact_current_budget)
    assert bounded.configured_max_bytes == exact_current_budget
    resume.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    error = outcome.get("error")
    assert isinstance(error, CollectorStorageBackpressureError)
    assert "RETENTION_REQUIRED" in str(error)
    assert writer.get("delta-2") is None
    assert bounded.get("delta-1") == first
    assert path.stat().st_size == before_size
    _, after_pages = _page_geometry(path)
    assert after_pages <= page_count


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
