"""R05 regression: collector byte budget must observe the live SQLite WAL.

SQLite max_page_count constrains logical database pages, but a WAL can retain many
historical page frames while a reader pins an old snapshot.  For a 24/7 collector,
an operator byte budget cannot be checked from the main database file alone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autosport.causal_collector import CollectorDelta, CollectorDeltaStore
from autosport.collector_service import (
    CollectorRetentionRequiredError,
    CollectorServiceConfig,
    HeadlessCollectorService,
)
from autosport.event_lifecycle import CatalogPage, ContinuousEventLifecycle


_INSTANT = "2026-09-22T07:40:00+00:00"
_BUDGET = 1024 * 1024


class _Source:
    source_id = "wal-budget-source"
    stream_epoch = "wal-budget-epoch"

    def fetch_catalog_page(self, _checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-wal-budget-epoch",
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, _checkpoint, _records, _max_items):
        return ()


def _delta(position: int) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=f"wal-budget-delta-{position}",
        source_id="wal-budget-source",
        lawful_terms_ref="terms-v1",
        retention_ref="retention-v1",
        stream_epoch="wal-budget-epoch",
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


def _wal_bytes(path: Path) -> int:
    wal = Path(f"{path}-wal")
    return wal.stat().st_size if wal.exists() else 0


def test_service_budget_detects_checkpoint_starved_wal_footprint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector.sqlite"
    store = CollectorDeltaStore(path, max_bytes=_BUDGET)
    service = HeadlessCollectorService(
        delta_store=store,
        lifecycle=ContinuousEventLifecycle(tmp_path / "catalog.json"),
        source=_Source(),
        state_path=tmp_path / "service.json",
        run_id="run-wal-budget",
        config=CollectorServiceConfig(
            max_store_bytes=_BUDGET,
            poll_interval_seconds=1,
        ),
        clock=lambda: _INSTANT,
        sleep=lambda _seconds: None,
    )

    reader = sqlite3.connect(path, isolation_level=None)
    try:
        # Pin a genuine old read snapshot.  New writer commits remain readable and
        # canonical, but their WAL frames cannot all be checkpointed away.
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM collector_deltas").fetchone()

        exceeded = False
        for position in range(1, 257):
            assert store.append(_delta(position)) is True
            db_bytes = path.stat().st_size
            wal_bytes = _wal_bytes(path)
            if db_bytes < _BUDGET and db_bytes + wal_bytes > _BUDGET:
                exceeded = True
                break

        assert exceeded, (
            "test setup failed to create a checkpoint-starved WAL larger than "
            "the remaining configured collector byte budget"
        )
        assert path.stat().st_size < _BUDGET
        assert path.stat().st_size + _wal_bytes(path) > _BUDGET

        # The operator-visible service budget is a physical storage guard.  Looking
        # only at path.stat().st_size misses the live WAL and incorrectly permits
        # continued collection while the configured footprint is already exceeded.
        with pytest.raises(
            CollectorRetentionRequiredError,
            match="RETENTION_REQUIRED",
        ):
            service._check_storage_budget()
    finally:
        reader.rollback()
        reader.close()
