from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.providers import InMemoryProvider, ProviderQuote
from autosport.storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class MarketStoreIOProfile:
    requested: int
    received: int
    accepted: int
    rejected: int
    history_events: int
    current_quotes: int
    sqlite_file_sizes: tuple[tuple[str, int], ...]
    durable_footprint_bytes: int
    ingest_elapsed_seconds: float
    reopen_elapsed_seconds: float
    history_read_elapsed_seconds: float
    current_read_elapsed_seconds: float

    @property
    def accepted_per_second(self) -> float:
        _positive_elapsed("ingest_elapsed_seconds", self.ingest_elapsed_seconds)
        return self.accepted / self.ingest_elapsed_seconds

    @property
    def bytes_per_accepted_event(self) -> float:
        if self.accepted <= 0:
            raise ValueError("accepted must be positive before computing bytes/event")
        if self.durable_footprint_bytes <= 0:
            raise ValueError("durable_footprint_bytes must be positive")
        return self.durable_footprint_bytes / self.accepted


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_elapsed(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return numeric


def _current_projection_snapshot(
    current: dict[tuple[str, str], MarketEvent],
) -> tuple[tuple[str, str, str], ...]:
    """Detach exact current identity+payload for restart equality proof."""

    rows: list[tuple[str, str, str]] = []
    for key, event in current.items():
        if (
            type(key) is not tuple
            or len(key) != 2
            or type(key[0]) is not str
            or type(key[1]) is not str
            or type(event) is not MarketEvent
        ):
            raise RuntimeError("current projection contains a non-canonical entry")
        source_id, quote_key = key
        if (event.source_id, event.quote_key) != key:
            raise RuntimeError(
                "current projection key does not match canonical event identity"
            )
        try:
            payload = json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "current projection payload is not canonical JSON"
            ) from exc
        rows.append((source_id, quote_key, payload))
    return tuple(sorted(rows))


def _build_quotes(count: int) -> list[ProviderQuote]:
    count = _positive_int("count", count)
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        ProviderQuote(
            provider_event_id=f"event-{index % 50}",
            provider_market_id="winner",
            provider_selection_id=f"selection-{index % 100}",
            decimal_odds=Decimal("1.80"),
            observed_ts=(origin + timedelta(seconds=index)).isoformat(),
            sequence=index,
        )
        for index in range(count)
    ]


def _sqlite_footprint(path: Path) -> tuple[tuple[tuple[str, int], ...], int]:
    if not path.is_file():
        raise RuntimeError(f"SQLite database was not durably published: {path}")
    candidates = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
    files: list[tuple[str, int]] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        if not candidate.is_file():
            raise RuntimeError(f"SQLite footprint member is not a file: {candidate}")
        size = candidate.stat().st_size
        if size < 0:
            raise RuntimeError(f"SQLite footprint member has invalid size: {candidate}")
        files.append((candidate.name, size))
    total = sum(size for _name, size in files)
    if total <= 0:
        raise RuntimeError("SQLite durable footprint must be non-empty")
    return tuple(files), total


def run_profile(count: int = 20_000, batch_size: int = 1_000) -> MarketStoreIOProfile:
    count = _positive_int("count", count)
    batch_size = _positive_int("batch_size", batch_size)
    quotes = _build_quotes(count)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "market-store-io.db"
        store = SQLiteMarketStore(path)
        try:
            bus = MarketEventBus(store)
            engine = IngestionEngine(bus)
            provider = InMemoryProvider("benchmark-market-store-io", quotes)
            received = 0
            accepted = 0
            rejected = 0

            ingest_started = time.perf_counter()
            while received < count:
                stats = engine.poll_once(provider, max_items=batch_size)
                if stats.received == 0:
                    raise RuntimeError(
                        "benchmark provider exhausted before the requested workload completed: "
                        f"requested={count} received={received} accepted={accepted} "
                        f"rejected={rejected}"
                    )
                received += stats.received
                accepted += stats.accepted
                rejected += stats.rejected
            ingest_elapsed = time.perf_counter() - ingest_started

            current_before_close = _current_projection_snapshot(
                store.current_by_source()
            )
        finally:
            store.close()

        file_sizes, durable_bytes = _sqlite_footprint(path)

        reopen_started = time.perf_counter()
        reopened = SQLiteMarketStore(path)
        reopen_elapsed = time.perf_counter() - reopen_started
        try:
            history_started = time.perf_counter()
            history = reopened.events()
            history_elapsed = time.perf_counter() - history_started

            current_started = time.perf_counter()
            current = reopened.current_by_source()
            current_elapsed = time.perf_counter() - current_started
        finally:
            reopened.close()

        for field_name, elapsed in (
            ("ingest_elapsed_seconds", ingest_elapsed),
            ("reopen_elapsed_seconds", reopen_elapsed),
            ("history_read_elapsed_seconds", history_elapsed),
            ("current_read_elapsed_seconds", current_elapsed),
        ):
            _positive_elapsed(field_name, elapsed)

        if received != count or accepted != count or rejected != 0:
            raise RuntimeError(
                "benchmark workload was not fully accepted; I/O profile would be misleading: "
                f"requested={count} received={received} accepted={accepted} rejected={rejected}"
            )
        if len(history) != accepted:
            raise RuntimeError(
                "reopened durable history count does not match accepted workload: "
                f"accepted={accepted} history={len(history)}"
            )
        current_after_reopen = _current_projection_snapshot(current)
        if current_after_reopen != current_before_close:
            raise RuntimeError(
                "reopened current projection content changed across close/reopen"
            )

        result = MarketStoreIOProfile(
            requested=count,
            received=received,
            accepted=accepted,
            rejected=rejected,
            history_events=len(history),
            current_quotes=len(current),
            sqlite_file_sizes=file_sizes,
            durable_footprint_bytes=durable_bytes,
            ingest_elapsed_seconds=ingest_elapsed,
            reopen_elapsed_seconds=reopen_elapsed,
            history_read_elapsed_seconds=history_elapsed,
            current_read_elapsed_seconds=current_elapsed,
        )
        _ = result.accepted_per_second
        _ = result.bytes_per_accepted_event
        return result


def main(count: int = 20_000, batch_size: int = 1_000) -> None:
    result = run_profile(count=count, batch_size=batch_size)
    files = ",".join(f"{name}:{size}" for name, size in result.sqlite_file_sizes)
    print(
        f"requested={result.requested} received={result.received} "
        f"accepted={result.accepted} rejected={result.rejected} "
        f"history={result.history_events} current={result.current_quotes} "
        f"sqlite_files={files} durable_bytes={result.durable_footprint_bytes} "
        f"bytes_per_event={result.bytes_per_accepted_event:.1f} "
        f"ingest={result.ingest_elapsed_seconds:.6f}s "
        f"rate={result.accepted_per_second:.0f} events/s "
        f"reopen={result.reopen_elapsed_seconds:.6f}s "
        f"history_read={result.history_read_elapsed_seconds:.6f}s "
        f"current_read={result.current_read_elapsed_seconds:.6f}s"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the canonical durable SQLiteMarketStore footprint, reopen, "
            "history-read and current-projection read costs without applying a release threshold."
        )
    )
    parser.add_argument("--count", type=int, default=20_000, help="number of synthetic events")
    parser.add_argument("--batch-size", type=int, default=1_000, help="provider batch size")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(count=args.count, batch_size=args.batch_size)
