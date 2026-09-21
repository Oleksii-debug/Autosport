from __future__ import annotations

import argparse
import json
import math
import platform
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, TypeVar

from autosport.domain import MarketEvent, MarketType
from autosport.storage import SQLiteMarketStore


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class OperationMeasurement:
    operation: str
    rows: int
    elapsed_seconds: float

    @property
    def rows_per_second(self) -> float:
        if (
            not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds <= 0
        ):
            raise ValueError("elapsed_seconds must be finite and positive")
        if isinstance(self.rows, bool) or not isinstance(self.rows, int) or self.rows < 0:
            raise ValueError("rows must be a non-negative integer")
        return self.rows / self.elapsed_seconds


@dataclass(frozen=True, slots=True)
class StorageHistoryBenchmarkResult:
    schema_version: int
    requested_events: int
    persisted_events: int
    event_count: int
    selections_per_event: int
    target_event_id: str
    database_bytes: int
    python_version: str
    sqlite_version: str
    operating_system: str
    read_after_reopen: bool
    write: OperationMeasurement
    full_history_read: OperationMeasurement
    event_history_read: OperationMeasurement
    current_projection_read: OperationMeasurement

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["write"]["rows_per_second"] = self.write.rows_per_second
        payload["full_history_read"]["rows_per_second"] = (
            self.full_history_read.rows_per_second
        )
        payload["event_history_read"]["rows_per_second"] = (
            self.event_history_read.rows_per_second
        )
        payload["current_projection_read"]["rows_per_second"] = (
            self.current_projection_read.rows_per_second
        )
        return payload


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _build_events(
    count: int,
    *,
    event_count: int,
    selections_per_event: int,
) -> list[MarketEvent]:
    count = _positive_int("count", count)
    event_count = _positive_int("event_count", event_count)
    selections_per_event = _positive_int(
        "selections_per_event",
        selections_per_event,
    )
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events: list[MarketEvent] = []
    for index in range(count):
        event_index = index % event_count
        selection_index = (index // event_count) % selections_per_event
        observed_ts = (origin + timedelta(milliseconds=index)).isoformat()
        events.append(
            MarketEvent(
                event_id=f"event-{event_index}",
                market_id="winner",
                selection_id=f"selection-{selection_index}",
                decimal_odds=Decimal("1.80"),
                observed_ts=observed_ts,
                ingest_ts=observed_ts,
                source_id="benchmark",
                sequence=index + 1,
                market_type=MarketType.WINNER,
            )
        )
    return events


def _measure(operation: str, action: Callable[[], _T]) -> tuple[_T, float]:
    started = time.perf_counter()
    value = action()
    elapsed = time.perf_counter() - started
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise RuntimeError(
            f"{operation} produced invalid timing evidence: {elapsed!r}"
        )
    return value, elapsed


def run_benchmark(
    count: int = 100_000,
    *,
    event_count: int = 500,
    selections_per_event: int = 8,
) -> StorageHistoryBenchmarkResult:
    count = _positive_int("count", count)
    event_count = _positive_int("event_count", event_count)
    selections_per_event = _positive_int(
        "selections_per_event",
        selections_per_event,
    )
    events = _build_events(
        count,
        event_count=event_count,
        selections_per_event=selections_per_event,
    )
    target_event_id = events[0].event_id
    expected_target_rows = sum(
        1 for event in events if event.event_id == target_event_id
    )
    expected_current_rows = len(
        {(event.source_id, event.quote_key) for event in events}
    )

    with tempfile.TemporaryDirectory() as tmp:
        database_path = Path(tmp) / "market-history-benchmark.db"
        write_store = SQLiteMarketStore(database_path)
        try:
            persisted_events, write_elapsed = _measure(
                "write",
                lambda: write_store.append_many(events),
            )
            if persisted_events != count:
                raise RuntimeError(
                    "storage benchmark did not persist the complete workload: "
                    f"requested={count} persisted={persisted_events}"
                )
        finally:
            write_store.close()

        # Product endurance/restart consumers reopen the canonical database before
        # reading history. Drop the generated workload objects and use a fresh
        # SQLiteMarketStore so read timing cannot reuse the writer connection cache.
        del events
        store = SQLiteMarketStore(database_path)
        try:
            full_history, full_elapsed = _measure(
                "full_history_read",
                store.events,
            )
            if len(full_history) != count:
                raise RuntimeError(
                    "full-history read returned an incomplete workload: "
                    f"expected={count} actual={len(full_history)}"
                )

            event_history, event_elapsed = _measure(
                "event_history_read",
                lambda: store.events(target_event_id),
            )
            if len(event_history) != expected_target_rows:
                raise RuntimeError(
                    "indexed event-history read returned an incomplete workload: "
                    f"event_id={target_event_id} expected={expected_target_rows} "
                    f"actual={len(event_history)}"
                )

            current_projection, current_elapsed = _measure(
                "current_projection_read",
                store.current,
            )
            if len(current_projection) != expected_current_rows:
                raise RuntimeError(
                    "current-projection read returned an incomplete workload: "
                    f"expected={expected_current_rows} actual={len(current_projection)}"
                )

            database_bytes = database_path.stat().st_size
            if database_bytes <= 0:
                raise RuntimeError("storage benchmark database is unexpectedly empty")

            result = StorageHistoryBenchmarkResult(
                schema_version=1,
                requested_events=count,
                persisted_events=persisted_events,
                event_count=event_count,
                selections_per_event=selections_per_event,
                target_event_id=target_event_id,
                database_bytes=database_bytes,
                python_version=platform.python_version(),
                sqlite_version=sqlite3.sqlite_version,
                operating_system=platform.system(),
                read_after_reopen=True,
                write=OperationMeasurement(
                    operation="write",
                    rows=persisted_events,
                    elapsed_seconds=write_elapsed,
                ),
                full_history_read=OperationMeasurement(
                    operation="full_history_read",
                    rows=len(full_history),
                    elapsed_seconds=full_elapsed,
                ),
                event_history_read=OperationMeasurement(
                    operation="event_history_read",
                    rows=len(event_history),
                    elapsed_seconds=event_elapsed,
                ),
                current_projection_read=OperationMeasurement(
                    operation="current_projection_read",
                    rows=len(current_projection),
                    elapsed_seconds=current_elapsed,
                ),
            )
            _ = result.write.rows_per_second
            _ = result.full_history_read.rows_per_second
            _ = result.event_history_read.rows_per_second
            _ = result.current_projection_read.rows_per_second
            return result
        finally:
            store.close()


def main(
    count: int = 100_000,
    *,
    event_count: int = 500,
    selections_per_event: int = 8,
) -> None:
    result = run_benchmark(
        count=count,
        event_count=event_count,
        selections_per_event=selections_per_event,
    )
    print(json.dumps(result.to_dict(), sort_keys=True))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure canonical SQLiteMarketStore write and read paths without "
            "inventing a performance pass/fail threshold."
        )
    )
    parser.add_argument(
        "--count",
        type=int,
        default=100_000,
        help="number of append-only market events",
    )
    parser.add_argument(
        "--event-count",
        type=int,
        default=500,
        help="number of event identities distributed through the history",
    )
    parser.add_argument(
        "--selections-per-event",
        type=int,
        default=8,
        help="number of repeating quote selections per event",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(
        count=args.count,
        event_count=args.event_count,
        selections_per_event=args.selections_per_event,
    )
