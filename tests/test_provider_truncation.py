import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.live_observation import poll_open_market_store_once
from autosport.market_bus import MarketEventBus
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import BoundedMirrorInvalidationBuffer
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider
from autosport.providers import ProviderBatch, ProviderQuote
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


    def test_early_causal_degradation_survives_truncated_snapshot_drain(self):
        class TwoChunkProvider:
            source_id = "fixture:two-chunk"

            def __init__(self):
                self.calls = 0

            def read_batch(self, max_items=1000):
                del max_items
                self.calls += 1
                if self.calls == 1:
                    return ProviderBatch(
                        self.source_id,
                        (
                            ProviderQuote(
                                provider_event_id="event-1",
                                provider_market_id="winner",
                                provider_selection_id="future",
                                decimal_odds=Decimal("2.0"),
                                observed_ts="2026-09-12T20:00:03+00:00",
                                sequence=1,
                            ),
                        ),
                        cursor="1",
                        quality_flags=("TRUNCATED_BATCH",),
                    )
                return ProviderBatch(
                    self.source_id,
                    (
                        ProviderQuote(
                            provider_event_id="event-1",
                            provider_market_id="winner",
                            provider_selection_id="valid",
                            decimal_odds=Decimal("2.1"),
                            observed_ts="2026-09-12T20:00:02+00:00",
                            sequence=2,
                        ),
                    ),
                    cursor="2",
                )

        with tempfile.TemporaryDirectory() as tmp:
            market = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                health = SourceHealthStore(Path(tmp) / "source-health.json")
                updates = BoundedMirrorInvalidationBuffer(MarketMirror.from_store(market))
                provider = TwoChunkProvider()

                stats = poll_open_market_store_once(
                    market,
                    health,
                    provider,
                    mirror_updates=updates,
                    max_items=1,
                    policy=IngestionPolicy(
                        max_batch_size=10,
                        stale_after_seconds=60,
                        max_future_skew_seconds=5,
                    ),
                    clock=lambda: "2026-09-12T20:00:02+00:00",
                )

                self.assertEqual(provider.calls, 2)
                self.assertEqual(stats.received, 2)
                self.assertEqual(stats.accepted, 1)
                self.assertEqual(stats.rejected, 1)
                self.assertEqual(
                    stats.quality_flags,
                    ("FUTURE_OBSERVATION_TIMESTAMP",),
                )
                state = health.get(provider.source_id)
                self.assertEqual(state.status, "degraded")
                self.assertEqual(
                    state.quality_flags,
                    ("FUTURE_OBSERVATION_TIMESTAMP",),
                )
                persisted = market.events()
                self.assertEqual(len(persisted), 1)
                self.assertEqual(
                    persisted[0].selection_id,
                    "fixture:two-chunk:valid",
                )
            finally:
                market.close()


if __name__ == "__main__":
    unittest.main()
