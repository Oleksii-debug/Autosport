import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.live_observation import observe_workspace_once
from autosport.market_bus import MarketEventBus
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider
from autosport.providers import ProviderBatch, ProviderQuote
from autosport.storage import SQLiteMarketStore


class ParlayApiBoundedSnapshotDrainTests(unittest.TestCase):
    @staticmethod
    def _event(event_id: str, selections: list[str]) -> dict:
        return {
            "id": event_id,
            "bookmakers": [
                {
                    "key": "book-a",
                    "last_update": "2026-09-14T08:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-09-14T08:00:00Z",
                            "outcomes": [
                                {"name": selection, "price": 2.0}
                                for selection in selections
                            ],
                        }
                    ],
                }
            ],
        }

    def test_bounded_reads_drain_one_fetched_snapshot_before_refetch(self):
        calls: list[str] = []
        payloads = iter(
            [
                [self._event("event-1", ["A", "B", "C", "D", "E"])],
                [self._event("event-2", ["F"])],
            ]
        )
        clock_values = iter(
            [
                "2026-09-14T08:00:10+00:00",
                "2026-09-14T08:00:20+00:00",
            ]
        )

        def transport(url, headers, timeout):
            calls.append(url)
            return HttpJsonResponse(next(payloads), 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            clock=lambda: next(clock_values),
        )

        first = provider.read_batch(max_items=2)
        second = provider.read_batch(max_items=2)
        third = provider.read_batch(max_items=2)

        self.assertEqual(len(calls), 1)
        self.assertEqual([len(first.quotes), len(second.quotes), len(third.quotes)], [2, 2, 1])
        self.assertEqual(first.quality_flags, ("TRUNCATED_BATCH",))
        self.assertEqual(second.quality_flags, ("TRUNCATED_BATCH",))
        self.assertEqual(third.quality_flags, ())
        self.assertEqual(
            [quote.provider_selection_id for batch in (first, second, third) for quote in batch.quotes],
            ["A", "B", "C", "D", "E"],
        )
        self.assertEqual(
            {first.cursor, second.cursor, third.cursor},
            {"2026-09-14T08:00:10+00:00"},
        )

        next_snapshot = provider.read_batch(max_items=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual([quote.provider_event_id for quote in next_snapshot.quotes], ["event-2"])
        self.assertEqual(next_snapshot.cursor, "2026-09-14T08:00:20+00:00")
        self.assertEqual(next_snapshot.quality_flags, ())

    def test_exact_limit_completes_snapshot_without_false_truncation(self):
        calls: list[str] = []
        payloads = iter(
            [
                [self._event("event-1", ["A", "B"])],
                [self._event("event-2", ["C"])],
            ]
        )
        clock_values = iter(
            [
                "2026-09-14T08:00:10+00:00",
                "2026-09-14T08:00:20+00:00",
            ]
        )

        def transport(url, headers, timeout):
            calls.append(url)
            return HttpJsonResponse(next(payloads), 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            clock=lambda: next(clock_values),
        )

        exact = provider.read_batch(max_items=2)
        self.assertEqual(len(exact.quotes), 2)
        self.assertEqual(exact.quality_flags, ())
        self.assertEqual(len(calls), 1)

        next_snapshot = provider.read_batch(max_items=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual([quote.provider_event_id for quote in next_snapshot.quotes], ["event-2"])
        self.assertEqual(next_snapshot.cursor, "2026-09-14T08:00:20+00:00")

    def test_ingestion_persists_every_quote_from_bounded_snapshot(self):
        calls: list[str] = []
        payload = [self._event("event-1", ["A", "B", "C", "D", "E"])]

        def transport(url, headers, timeout):
            calls.append(url)
            return HttpJsonResponse(payload, 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            clock=lambda: "2026-09-14T08:00:10+00:00",
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                engine = IngestionEngine(
                    MarketEventBus(store),
                    clock=lambda: "2026-09-14T08:00:10+00:00",
                )
                first = engine.poll_once(provider, max_items=2)
                second = engine.poll_once(provider, max_items=2)
                third = engine.poll_once(provider, max_items=2)

                self.assertEqual([first.accepted, second.accepted, third.accepted], [2, 2, 1])
                self.assertEqual(first.quality_flags, ("TRUNCATED_BATCH",))
                self.assertEqual(second.quality_flags, ("TRUNCATED_BATCH",))
                self.assertEqual(third.quality_flags, ())
                self.assertEqual(len(calls), 1)
                events = store.events()
                self.assertEqual(len(events), 5)
                self.assertEqual(
                    {event.selection_id for event in events},
                    {
                        "parlayapi:table_tennis:A",
                        "parlayapi:table_tennis:B",
                        "parlayapi:table_tennis:C",
                        "parlayapi:table_tennis:D",
                        "parlayapi:table_tennis:E",
                    },
                )
            finally:
                store.close()

    def test_live_drain_preserves_earlier_substantive_degradation_in_durable_health(self):
        class ChunkedProvider:
            source_id = "chunked-health-test"

            def __init__(self) -> None:
                self.calls = 0

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                self.calls += 1
                if self.calls > 2:
                    raise AssertionError("snapshot drain fetched beyond terminal chunk")
                quote = ProviderQuote(
                    provider_event_id=f"event-{self.calls}",
                    provider_market_id="market",
                    provider_selection_id=f"selection-{self.calls}",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-14T08:00:10+00:00",
                    source_ts="2026-09-14T08:00:10+00:00",
                    sequence=self.calls,
                )
                flags = (
                    ("UPSTREAM_PARTIAL", "TRUNCATED_BATCH")
                    if self.calls == 1
                    else ()
                )
                return ProviderBatch(
                    self.source_id,
                    (quote,),
                    cursor=f"chunk-{self.calls}",
                    quality_flags=flags,
                )

        provider = ChunkedProvider()
        with tempfile.TemporaryDirectory() as tmp:
            result = observe_workspace_once(
                tmp,
                provider,
                max_items=1,
                clock=lambda: "2026-09-14T08:00:10+00:00",
            )

        self.assertEqual(provider.calls, 2)
        self.assertEqual(result.stats.quality_flags, ("UPSTREAM_PARTIAL",))
        self.assertEqual(result.stats.health_status, "degraded")
        self.assertEqual(result.health.status, "degraded")
        self.assertEqual(result.health.quality_flags, ("UPSTREAM_PARTIAL",))
        self.assertEqual(result.health.poll_count, 2)
        self.assertEqual(result.health.total_received, 2)
        self.assertNotIn("TRUNCATED_BATCH", result.health.quality_flags)


if __name__ == "__main__":
    unittest.main()
