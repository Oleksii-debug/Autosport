from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.providers import ProviderBatch, ProviderQuote
from autosport.storage import SQLiteMarketStore


class _MalformedQuotesProvider:
    source_id = "fixture"

    def __init__(self, quotes: object) -> None:
        self.quotes = quotes

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        del max_items
        return ProviderBatch(self.source_id, self.quotes)  # type: ignore[arg-type]


def _quote() -> ProviderQuote:
    return ProviderQuote(
        provider_event_id="event-1",
        provider_market_id="winner",
        provider_selection_id="player-a",
        decimal_odds=Decimal("2.0"),
        observed_ts="2026-09-14T09:10:00+00:00",
        sequence=1,
    )


class ProviderBatchQuoteContractTests(unittest.TestCase):
    def test_batch_rejects_mutable_quote_container(self) -> None:
        with self.assertRaisesRegex(TypeError, "quotes must be a tuple"):
            ProviderBatch("fixture", [_quote()])  # type: ignore[arg-type]

    def test_batch_rejects_non_quote_tuple_member(self) -> None:
        with self.assertRaisesRegex(TypeError, "quote must be ProviderQuote"):
            ProviderBatch("fixture", (_quote(), object()))  # type: ignore[arg-type]

    def test_malformed_quote_container_fails_before_market_persistence(self) -> None:
        cases = (
            ([_quote()], "quotes must be a tuple"),
            ((_quote(), object()), "quote must be ProviderQuote"),
        )
        for quotes, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = SQLiteMarketStore(root / "market.db")
                try:
                    health = SourceHealthStore(root / "source-health.json")
                    engine = IngestionEngine(
                        MarketEventBus(store),
                        health_store=health,
                        clock=lambda: "2026-09-14T09:10:01+00:00",
                    )

                    with self.assertRaisesRegex(TypeError, message):
                        engine.poll_once(_MalformedQuotesProvider(quotes), max_items=10)

                    self.assertEqual(store.events(), [])
                    state = health.get("fixture")
                    self.assertEqual(state.status, "failed")
                    self.assertEqual(state.poll_count, 1)
                    self.assertEqual(state.total_failures, 1)
                    self.assertEqual(state.total_received, 0)
                    self.assertEqual(state.total_accepted, 0)
                    self.assertEqual(state.total_rejected, 0)
                    self.assertIsNone(state.last_cursor)
                finally:
                    store.close()

    def test_canonical_tuple_of_provider_quotes_remains_valid(self) -> None:
        quote = _quote()
        batch = ProviderBatch("fixture", (quote,), cursor="1")

        self.assertEqual(batch.quotes, (quote,))
        self.assertEqual(batch.cursor, "1")


if __name__ == "__main__":
    unittest.main()
