import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.market_bus import MarketEventBus, MarketEventDeliveryError
from autosport.providers import ProviderBatch, ProviderQuote
from autosport.storage import SQLiteMarketStore


class BatchProvider:
    def __init__(self, source_id: str, batches: list[ProviderBatch]) -> None:
        self.source_id = source_id
        self._batches = list(batches)

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if not self._batches:
            return ProviderBatch(self.source_id, tuple(), cursor="empty")
        return self._batches.pop(0)


class IngestionDuplicateObservabilityTests(unittest.TestCase):
    def _engine(self, tmp: str):
        store = SQLiteMarketStore(Path(tmp) / "market.db")
        bus = MarketEventBus(store)
        health = SourceHealthStore(Path(tmp) / "source-health.json")
        origin = datetime.fromisoformat("2026-09-21T12:00:00+00:00")
        tick = 0

        def clock() -> str:
            nonlocal tick
            tick += 1
            return (origin + timedelta(seconds=tick)).isoformat()

        engine = IngestionEngine(
            bus,
            policy=IngestionPolicy(
                max_batch_size=100,
                stale_after_seconds=60,
                max_future_skew_seconds=5,
            ),
            health_store=health,
            clock=clock,
        )
        return engine, bus, store, health

    @staticmethod
    def _quote(selection: str, sequence: int) -> ProviderQuote:
        return ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="winner",
            provider_selection_id=selection,
            decimal_odds=Decimal("2.0"),
            observed_ts="2026-09-21T12:00:00+00:00",
            sequence=sequence,
        )

    def test_exact_storage_duplicate_is_visible_as_nonaccepted_evidence(self):
        quote = self._quote("a", 1)
        provider = BatchProvider(
            "provider-a",
            [
                ProviderBatch("provider-a", (quote,), cursor="1"),
                ProviderBatch("provider-a", (quote,), cursor="2"),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine, _bus, store, health = self._engine(tmp)
            first = engine.poll_once(provider, max_items=10)
            second = engine.poll_once(provider, max_items=10)

            self.assertEqual((first.received, first.accepted, first.rejected), (1, 1, 0))
            self.assertEqual(first.health_status, "healthy")
            self.assertNotIn("DUPLICATE_EVENT", first.quality_flags)

            self.assertEqual((second.received, second.accepted, second.rejected), (1, 0, 1))
            self.assertEqual(second.quality_flags, ("DUPLICATE_EVENT",))
            self.assertEqual(second.health_status, "degraded")

            persisted = health.get("provider-a")
            self.assertEqual(persisted.poll_count, 2)
            self.assertEqual(persisted.total_received, 2)
            self.assertEqual(persisted.total_accepted, 1)
            self.assertEqual(persisted.total_rejected, 1)
            self.assertEqual(
                persisted.total_accepted + persisted.total_rejected,
                persisted.total_received,
            )
            self.assertEqual(persisted.quality_flags, ("DUPLICATE_EVENT",))
            self.assertEqual(persisted.status, "degraded")
            store.close()

    def test_duplicate_accounting_survives_post_persistence_delivery_failure(self):
        first_quote = self._quote("a", 1)
        second_quote = self._quote("b", 2)
        provider = BatchProvider(
            "provider-b",
            [
                ProviderBatch("provider-b", (first_quote,), cursor="1"),
                ProviderBatch(
                    "provider-b",
                    (first_quote, second_quote),
                    cursor="2",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine, bus, store, health = self._engine(tmp)
            engine.poll_once(provider, max_items=10)

            def fail_delivery(_event) -> None:
                raise RuntimeError("subscriber failed")

            bus.subscribe(fail_delivery)
            with self.assertRaises(MarketEventDeliveryError):
                engine.poll_once(provider, max_items=10)

            persisted = health.get("provider-b")
            self.assertEqual(persisted.poll_count, 2)
            self.assertEqual(persisted.total_received, 3)
            self.assertEqual(persisted.total_accepted, 2)
            self.assertEqual(persisted.total_rejected, 1)
            self.assertEqual(
                persisted.total_accepted + persisted.total_rejected,
                persisted.total_received,
            )
            self.assertEqual(persisted.quality_flags, ("DUPLICATE_EVENT",))
            self.assertEqual(persisted.status, "degraded")
            self.assertEqual(len(store.events()), 2)
            store.close()


if __name__ == "__main__":
    unittest.main()
