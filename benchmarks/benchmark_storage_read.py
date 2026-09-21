from __future__ import annotations

import argparse
import math
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class StorageReadBenchmarkResult:
    requested_history_rows: int
    history_rows: int
    current_projection_rows: int
    filtered_event_id: str
    filtered_event_rows: int
    reopen_seconds: float
    current_read_seconds: float
    filtered_event_read_seconds: float
    full_history_read_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "requested_history_rows",
            "history_rows",
            "current_projection_rows",
            "filtered_event_rows",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.requested_history_rows <= 0:
            raise ValueError("requested_history_rows must be positive")
        if self.history_rows != self.requested_history_rows:
            raise ValueError("history_rows must equal requested_history_rows")
        if not self.filtered_event_id or self.filtered_event_id != self.filtered_event_id.strip():
            raise ValueError("filtered_event_id must be canonical non-empty text")
        if self.current_projection_rows <= 0 or self.current_projection_rows > self.history_rows:
            raise ValueError("current_projection_rows must be within persisted history")
        if self.filtered_event_rows <= 0 or self.filtered_event_rows > self.history_rows:
            raise ValueError("filtered_event_rows must be within persisted history")
        for name in (
            "reopen_seconds",
            "current_read_seconds",
            "filtered_event_read_seconds",
            "full_history_read_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

    @property
    def full_history_rows_per_second(self) -> float:
        return self.history_rows / self.full_history_read_seconds

    @property
    def filtered_event_rows_per_second(self) -> float:
        return self.filtered_event_rows / self.filtered_event_read_seconds


def _positive_int(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _build_events(count: int) -> list[MarketEvent]:
    """Build a deterministic history with both projection overwrites and many event ids."""

    count = _positive_int("count", count)
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        MarketEvent.from_dict(
            {
                "event_id": f"event-{index % 50}",
                "market_id": "winner",
                "selection_id": f"selection-{index % 4}",
                "decimal_odds": "1.80",
                "observed_ts": (origin + timedelta(seconds=index)).isoformat(),
                "source_id": f"source-{index % 2}",
                "sequence": index,
            }
        )
        for index in range(count)
    ]


def _elapsed(started: float) -> float:
    elapsed = time.perf_counter() - started
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise RuntimeError("benchmark timer did not produce a finite positive duration")
    return elapsed


def run_benchmark(count: int = 50_000) -> StorageReadBenchmarkResult:
    """Measure SQLiteMarketStore read/reopen paths after deterministic population.

    Population is deliberately excluded from all reported timings.  Reopen includes
    the product's current startup behavior, including current-quote projection repair.
    Subsequent read timings are warm post-reopen reads and are reported separately.
    No pass/fail performance threshold is asserted by this evidence-only benchmark.
    """

    count = _positive_int("count", count)
    events = _build_events(count)
    target_event_id = "event-0"
    expected_history_ids = {event.dedupe_key for event in events}
    expected_current_keys = {(event.source_id, event.quote_key) for event in events}
    expected_filtered_ids = {
        event.dedupe_key for event in events if event.event_id == target_event_id
    }
    if len(expected_history_ids) != count:
        raise RuntimeError("benchmark fixture unexpectedly contains duplicate durable identities")
    if not expected_filtered_ids:
        raise RuntimeError("benchmark fixture does not contain the filtered event id")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "storage-read-benchmark.db"
        initial = SQLiteMarketStore(path)
        try:
            accepted = initial.append_many(events)
            if accepted != count:
                raise RuntimeError(
                    "benchmark population was not fully accepted: "
                    f"requested={count} accepted={accepted}"
                )
        finally:
            initial.close()

        started = time.perf_counter()
        reopened = SQLiteMarketStore(path)
        reopen_seconds = _elapsed(started)
        try:
            started = time.perf_counter()
            current = reopened.current_by_source()
            current_read_seconds = _elapsed(started)

            started = time.perf_counter()
            filtered = reopened.events(target_event_id)
            filtered_event_read_seconds = _elapsed(started)

            started = time.perf_counter()
            history = reopened.events()
            full_history_read_seconds = _elapsed(started)
        finally:
            reopened.close()

    history_ids = {event.dedupe_key for event in history}
    filtered_ids = {event.dedupe_key for event in filtered}
    if history_ids != expected_history_ids:
        raise RuntimeError("full-history read did not preserve the populated durable identities")
    if set(current) != expected_current_keys:
        raise RuntimeError("current projection read did not preserve expected provider/quote keys")
    if filtered_ids != expected_filtered_ids:
        raise RuntimeError("event-filter read did not preserve the expected durable identities")

    return StorageReadBenchmarkResult(
        requested_history_rows=count,
        history_rows=len(history),
        current_projection_rows=len(current),
        filtered_event_id=target_event_id,
        filtered_event_rows=len(filtered),
        reopen_seconds=reopen_seconds,
        current_read_seconds=current_read_seconds,
        filtered_event_read_seconds=filtered_event_read_seconds,
        full_history_read_seconds=full_history_read_seconds,
    )


def main(count: int = 50_000) -> None:
    result = run_benchmark(count=count)
    print(
        f"history_rows={result.history_rows} "
        f"current_rows={result.current_projection_rows} "
        f"filtered_event_id={result.filtered_event_id} "
        f"filtered_rows={result.filtered_event_rows}"
    )
    print(
        f"reopen={result.reopen_seconds:.6f}s "
        f"current_read={result.current_read_seconds:.6f}s "
        f"filtered_read={result.filtered_event_read_seconds:.6f}s "
        f"full_history_read={result.full_history_read_seconds:.6f}s"
    )
    print(
        f"filtered_rate={result.filtered_event_rows_per_second:.0f} rows/s "
        f"full_history_rate={result.full_history_rows_per_second:.0f} rows/s"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Autosport SQLiteMarketStore reopen and read paths without "
            "asserting a machine-specific performance threshold."
        )
    )
    parser.add_argument(
        "--count",
        type=int,
        default=50_000,
        help="number of deterministic market-history rows to populate before measurement",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(count=args.count)
