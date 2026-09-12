from __future__ import annotations

import tempfile
import time
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


def main(count: int = 50_000, batch: int = 1_000) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteMarketStore(Path(tmp) / "bench.db")
        started = time.perf_counter()
        for offset in range(0, count, batch):
            events = [
                MarketEvent.from_dict({
                    "event_id": f"event-{index % 50}",
                    "market_id": "winner",
                    "selection_id": f"player-{index % 100}",
                    "decimal_odds": "1.80",
                    "observed_ts": f"2026-01-01T00:{(index // 60) % 60:02d}:{index % 60:02d}+00:00",
                    "source_id": "benchmark",
                    "sequence": index,
                })
                for index in range(offset, min(count, offset + batch))
            ]
            store.append_many(events)
        elapsed = time.perf_counter() - started
        print(f"events={count} elapsed={elapsed:.3f}s rate={count / elapsed:.0f} events/s")
        store.close()


if __name__ == "__main__":
    main()
