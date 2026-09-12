import tempfile
import unittest
from pathlib import Path

from autosport.domain import MarketType
from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)
from autosport.storage import SQLiteMarketStore


SAMPLE_EVENT = {
    "id": "tt-100",
    "sport_key": "table_tennis",
    "sport_title": "Table Tennis",
    "commence_time": "2026-09-12T21:00:00Z",
    "home_team": "Player A",
    "away_team": "Player B",
    "bookmakers": [
        {
            "key": "book-a",
            "title": "Book A",
            "last_update": "2026-09-12T20:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-09-12T20:00:01Z",
                    "outcomes": [
                        {"name": "Player A", "price": 1.80},
                        {"name": "Player B", "price": 2.05},
                    ],
                },
                {
                    "key": "spreads",
                    "last_update": "2026-09-12T20:00:02Z",
                    "outcomes": [
                        {"name": "Player A", "price": 1.91, "point": -1.5},
                        {"name": "Player B", "price": 1.91, "point": 1.5},
                    ],
                },
                {
                    "key": "totals",
                    "last_update": "2026-09-12T20:00:03Z",
                    "outcomes": [
                        {"name": "Over", "price": 1.87, "point": 74.5},
                        {"name": "Under", "price": 1.95, "point": 74.5},
                    ],
                },
            ],
        }
    ],
}


class ParlayApiProviderTests(unittest.TestCase):
    def test_authenticated_snapshot_maps_to_typed_provider_quotes_without_key_in_url(self):
        calls = []

        def transport(url, headers, timeout):
            calls.append((url, dict(headers), timeout))
            return HttpJsonResponse([SAMPLE_EVENT], 200, {})

        provider = ParlayApiTableTennisProvider(
            "secret-key",
            transport=transport,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        batch = provider.read_batch()
        self.assertEqual(batch.source_id, "parlayapi:table_tennis")
        self.assertEqual(len(batch.quotes), 6)
        self.assertNotIn("secret-key", calls[0][0])
        self.assertEqual(calls[0][1]["X-API-Key"], "secret-key")
        self.assertIn("oddsFormat=decimal", calls[0][0])
        self.assertEqual(batch.quotes[0].market_type, MarketType.WINNER)
        self.assertEqual(batch.quotes[2].market_type, MarketType.HANDICAP)
        self.assertEqual(batch.quotes[4].market_type, MarketType.TOTAL)
        self.assertEqual(batch.quotes[2].provider_market_id, "book-a:spreads:1.5")
        self.assertEqual(batch.quotes[3].provider_market_id, "book-a:spreads:1.5")
        self.assertEqual(batch.quotes[0].source_ts, "2026-09-12T20:00:01Z")
        self.assertEqual(batch.quotes[0].metadata["bookmaker_key"], "book-a")
        self.assertFalse(batch.quotes[0].metadata["public_preview"])

    def test_public_preview_wrapper_uses_same_parser_without_api_key(self):
        seen = {}

        def transport(url, headers, timeout):
            seen["url"] = url
            seen["headers"] = dict(headers)
            return HttpJsonResponse({"events_returned": 1, "events": [SAMPLE_EVENT]}, 200, {})

        provider = ParlayApiTableTennisProvider(
            public_preview=True,
            transport=transport,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        batch = provider.read_batch(max_items=2)
        self.assertEqual(len(batch.quotes), 2)
        self.assertIn("/v1/try/table_tennis/odds", seen["url"])
        self.assertNotIn("X-API-Key", seen["headers"])
        self.assertTrue(batch.quotes[0].metadata["public_preview"])

    def test_unchanged_source_snapshot_is_idempotent_across_poll_receive_times(self):
        fetch_times = iter(["2026-09-12T20:00:10+00:00", "2026-09-12T20:00:20+00:00"])

        def transport(url, headers, timeout):
            return HttpJsonResponse([SAMPLE_EVENT], 200, {})

        provider = ParlayApiTableTennisProvider("key", transport=transport, clock=lambda: next(fetch_times))
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            engine = IngestionEngine(MarketEventBus(store))
            first = engine.poll_once(provider)
            second = engine.poll_once(provider)
            self.assertEqual(first.accepted, 6)
            self.assertEqual(second.accepted, 0)
            self.assertEqual(len(store.events()), 6)
            store.close()

    def test_rate_limit_retry_is_bounded_and_honors_capped_retry_after(self):
        attempts = []
        sleeps = []

        def transport(url, headers, timeout):
            attempts.append(url)
            if len(attempts) == 1:
                raise ProviderTransportError("rate limited", 429, 30.0)
            return HttpJsonResponse([SAMPLE_EVENT], 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            sleeper=sleeps.append,
            max_attempts=2,
            max_backoff_seconds=0.5,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        provider.read_batch(max_items=1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [0.5])

    def test_malformed_payload_fails_closed(self):
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse({"unexpected": []}, 200, {}),
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        with self.assertRaises(ProviderPayloadError):
            provider.read_batch()


if __name__ == "__main__":
    unittest.main()
