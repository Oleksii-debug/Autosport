from __future__ import annotations

import tempfile
import time
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.providers import InMemoryProvider, ProviderQuote
from autosport.storage import SQLiteMarketStore


def main(count: int = 50_000, batch_size: int = 1_000) -> None:
    quotes = [
        ProviderQuote(
            provider_event_id=f"event-{index % 50}",
            provider_market_id="winner",
            provider_selection_id=f"selection-{index % 100}",
            decimal_odds=Decimal("1.80"),
            observed_ts=f"2026-01-01T00:{(index // 60) % 60:02d}:{index % 60:02d}+00:00",
            sequence=index,
        )
        for index in range(count)
    ]
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteMarketStore(Path(tmp) / "bench.db")
        bus = MarketEventBus(store)
        engine = IngestionEngine(bus)
        provider = InMemoryProvider("benchmark", quotes)
        accepted = 0
        started = time.perf_counter()
        while accepted < count:
            stats = engine.poll_once(provider, max_items=batch_size)
            if stats.received == 0:
                break
            accepted += stats.accepted
        elapsed = time.perf_counter() - started
        print(f"events={accepted} elapsed={elapsed:.3f}s rate={accepted / elapsed:.0f} events/s")
        store.close()


if __name__ == "__main__":
    main()
