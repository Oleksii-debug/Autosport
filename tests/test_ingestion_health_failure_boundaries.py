import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.market_bus import MarketEventBus, MarketEventDeliveryError
from autosport.providers import ProviderBatch, ProviderQuote
from autosport.storage import SQLiteMarketStore


class StaticProvider:
    def __init__(self, source_id: str, batches: list[ProviderBatch]) -> None:
        self.source_id = source_id
        self.batches = list(batches)
        self.calls = 0

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        self.calls += 1
        if not self.batches:
            return ProviderBatch(self.source_id, tuple(), cursor="empty")
        return self.batches.pop(0)


def _quote(
    source_ts: str,
    *,
    sequence: int,
    selection: str,
    decimal_odds: Decimal = Decimal("2.0"),
) -> ProviderQuote:
    return ProviderQuote(
        provider_event_id="event-1",
        provider_market_id="winner",
        provider_selection_id=selection,
        decimal_odds=decimal_odds,
        observed_ts="2026-09-12T12:00:00+00:00",
        sequence=sequence,
        source_ts=source_ts,
    )


class IngestionHealthFailureBoundaryTests(unittest.TestCase):
    def _engine(self, tmp: str):
        store = SQLiteMarketStore(Path(tmp) / "market.db")
        bus = MarketEventBus(store)
        health = SourceHealthStore(Path(tmp) / "source-health.json")
        engine = IngestionEngine(
            bus,
            policy=IngestionPolicy(
                max_batch_size=100,
                stale_after_seconds=60,
                max_future_skew_seconds=5,
            ),
            health_store=health,
            clock=lambda: "2026-09-12T12:00:00+00:00",
        )
        return engine, bus, store, health

    def test_post_persistence_delivery_failure_records_exact_provider_success_before_reraising(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, bus, store, health = self._engine(tmp)
            first_quote = _quote(
                "2026-09-12T11:59:50+00:00",
                sequence=1,
                selection="a",
            )
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch(
                        "source",
                        (first_quote,),
                        cursor="1",
                    ),
                    ProviderBatch(
                        "source",
                        (
                            first_quote,
                            _quote(
                                "2026-09-12T11:59:51+00:00",
                                sequence=2,
                                selection="b",
                            ),
                        ),
                        cursor="2",
                    ),
                ],
            )

            first = engine.poll_once(provider, max_items=10)
            self.assertEqual(first.accepted, 1)
            self.assertEqual(health.get("source").status, "healthy")

            def fail_consumer(_event):
                raise RuntimeError("consumer failed after persistence")

            bus.subscribe(fail_consumer)
            with self.assertRaises(MarketEventDeliveryError) as raised:
                engine.poll_once(provider, max_items=10)

            self.assertEqual(raised.exception.accepted_count, 1)
            self.assertEqual(len(raised.exception.exceptions), 1)
            self.assertIsInstance(raised.exception.exceptions[0], RuntimeError)
            self.assertEqual(
                str(raised.exception.exceptions[0]),
                "consumer failed after persistence",
            )

            state = health.get("source")
            self.assertEqual(state.status, "healthy")
            self.assertEqual(state.poll_count, 2)
            self.assertEqual(state.total_received, 3)
            self.assertEqual(state.total_accepted, 2)
            self.assertEqual(state.total_rejected, 0)
            self.assertEqual(state.total_failures, 0)
            self.assertEqual(state.consecutive_failures, 0)
            self.assertEqual(state.last_cursor, "2")
            self.assertEqual(
                state.latest_source_ts,
                "2026-09-12T11:59:51+00:00",
            )
            self.assertEqual(len(store.events()), 2)
            store.close()

    def test_rejected_newer_quote_does_not_advance_source_time_high_water(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _bus, store, health = self._engine(tmp)
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch(
                        "source",
                        (
                            _quote(
                                "2026-09-12T11:59:55+00:00",
                                sequence=1,
                                selection="invalid",
                                decimal_odds=Decimal("NaN"),
                            ),
                        ),
                        cursor="1",
                    ),
                    ProviderBatch(
                        "source",
                        (_quote("2026-09-12T11:59:50+00:00", sequence=2, selection="valid"),),
                        cursor="2",
                    ),
                ],
            )

            first = engine.poll_once(provider, max_items=10)
            self.assertEqual(first.accepted, 0)
            self.assertEqual(first.rejected, 1)
            self.assertIsNone(health.get("source").latest_source_ts)

            second = engine.poll_once(provider, max_items=10)
            self.assertEqual(second.accepted, 1)
            self.assertNotIn("SOURCE_TIME_REGRESSION", second.quality_flags)
            self.assertEqual(
                health.get("source").latest_source_ts,
                "2026-09-12T11:59:50+00:00",
            )
            self.assertEqual(len(store.events()), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
