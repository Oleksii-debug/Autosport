from __future__ import annotations

import argparse
import math
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.providers import InMemoryProvider, ProviderQuote
from autosport.storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    requested: int
    received: int
    accepted: int
    rejected: int
    elapsed_seconds: float

    @property
    def accepted_per_second(self) -> float:
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds <= 0:
            raise ValueError("elapsed_seconds must be finite and positive")
        return self.accepted / self.elapsed_seconds


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


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


def run_benchmark(count: int = 50_000, batch_size: int = 1_000) -> BenchmarkResult:
    count = _positive_int("count", count)
    batch_size = _positive_int("batch_size", batch_size)
    quotes = _build_quotes(count)

    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteMarketStore(Path(tmp) / "bench.db")
        try:
            bus = MarketEventBus(store)
            engine = IngestionEngine(bus)
            provider = InMemoryProvider("benchmark", quotes)
            received = 0
            accepted = 0
            rejected = 0
            started = time.perf_counter()

            while received < count:
                stats = engine.poll_once(provider, max_items=batch_size)
                if stats.received == 0:
                    raise RuntimeError(
                        "benchmark provider exhausted before the requested workload completed: "
                        f"requested={count} received={received} accepted={accepted} rejected={rejected}"
                    )
                received += stats.received
                accepted += stats.accepted
                rejected += stats.rejected

            elapsed = time.perf_counter() - started
            result = BenchmarkResult(count, received, accepted, rejected, elapsed)
            if result.received != result.requested or result.accepted != result.requested or result.rejected:
                raise RuntimeError(
                    "benchmark workload was not fully accepted; throughput would be misleading: "
                    f"requested={result.requested} received={result.received} "
                    f"accepted={result.accepted} rejected={result.rejected}"
                )
            # Validate the timing evidence before returning a result that callers may
            # render as throughput.
            _ = result.accepted_per_second
            return result
        finally:
            store.close()


def main(count: int = 50_000, batch_size: int = 1_000) -> None:
    result = run_benchmark(count=count, batch_size=batch_size)
    print(
        f"requested={result.requested} received={result.received} "
        f"accepted={result.accepted} rejected={result.rejected} "
        f"elapsed={result.elapsed_seconds:.3f}s "
        f"rate={result.accepted_per_second:.0f} events/s"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic Autosport ingestion benchmark.")
    parser.add_argument("--count", type=int, default=50_000, help="number of synthetic events")
    parser.add_argument("--batch-size", type=int, default=1_000, help="provider batch size")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(count=args.count, batch_size=args.batch_size)
