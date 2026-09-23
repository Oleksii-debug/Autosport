import tempfile
import unittest
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider
from autosport.storage import SQLiteMarketStore


EVENT = {
    "id": "tt-truncation",
    "sport_key": "table_tennis",
    "bookmakers": [
        {
            "key": "book-a",
            "last_update": "2026-09-12T20:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-09-12T20:00:01Z",
                    "outcomes": [
                        {"name": "Player A", "price": 1.80},
                        {"name": "Player B", "price": 2.05},
                    ],
                }
            ],
        }
    ],
}


class ProviderTruncationTests(unittest.TestCase):
    @staticmethod
    def _provider():
        return ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse([EVENT], 200, {}),
            clock=lambda: "2026-09-12T20:00:02+00:00",
        )

    def test_truncation_flag_requires_an_actual_quote_beyond_limit(self):
        truncated = self._provider().read_batch(max_items=1)
        exact = self._provider().read_batch(max_items=2)
        self.assertEqual(len(truncated.quotes), 1)
        self.assertEqual(truncated.quality_flags, ("TRUNCATED_BATCH",))
        self.assertEqual(len(exact.quotes), 2)
        self.assertEqual(exact.quality_flags, ())

    def test_truncation_flows_into_persistent_source_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            market = SQLiteMarketStore(Path(tmp) / "market.db")
            health = SourceHealthStore(Path(tmp) / "source-health.json")
            engine = IngestionEngine(
                MarketEventBus(market),
                policy=IngestionPolicy(max_batch_size=10, stale_after_seconds=60, max_future_skew_seconds=5),
                health_store=health,
                clock=lambda: "2026-09-12T20:00:02+00:00",
            )
            stats = engine.poll_once(self._provider(), max_items=1)
            self.assertEqual(stats.accepted, 1)
            self.assertEqual(stats.health_status, "degraded")
            self.assertEqual(stats.quality_flags, ("TRUNCATED_BATCH",))
            state = health.get("parlayapi:table_tennis")
            self.assertEqual(state.status, "degraded")
            self.assertEqual(state.quality_flags, ("TRUNCATED_BATCH",))
            market.close()


if __name__ == "__main__":
    unittest.main()
