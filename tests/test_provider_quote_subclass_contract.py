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


class _BypassedProviderQuote(ProviderQuote):
    """Hostile subclass that skips base validation and rejects later member access."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "member_accesses", 0)

    def __getattribute__(self, name: str):
        if name in {"member_accesses", "__class__", "__dict__"}:
            return object.__getattribute__(self, name)
        try:
            accesses = object.__getattribute__(self, "member_accesses")
        except AttributeError:
            return object.__getattribute__(self, name)
        object.__setattr__(self, "member_accesses", accesses + 1)
        raise AssertionError(f"hostile ProviderQuote member accessed: {name}")


class _SubclassQuoteProvider:
    source_id = "fixture"

    def __init__(self, quote: ProviderQuote) -> None:
        self.quote = quote

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        del max_items
        return ProviderBatch(self.source_id, (self.quote,))


def _hostile_quote() -> _BypassedProviderQuote:
    return _BypassedProviderQuote(
        provider_event_id="forged:event",
        provider_market_id="winner|forged",
        provider_selection_id="player-a",
        decimal_odds=Decimal("2.0"),
        observed_ts="2026-09-15T17:20:00+00:00",
        sequence=1 << 63,
    )


class ProviderQuoteSubclassContractTests(unittest.TestCase):
    def test_batch_rejects_provider_quote_subclass_before_member_access(self) -> None:
        quote = _hostile_quote()

        with self.assertRaisesRegex(TypeError, "quote must be ProviderQuote"):
            ProviderBatch("fixture", (quote,))

        self.assertEqual(quote.member_accesses, 0)

    def test_subclass_rejection_precedes_persistence_and_success_progress(self) -> None:
        quote = _hostile_quote()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    health_store=health,
                    clock=lambda: "2026-09-15T17:20:01+00:00",
                )

                with self.assertRaisesRegex(TypeError, "quote must be ProviderQuote"):
                    engine.poll_once(_SubclassQuoteProvider(quote), max_items=10)

                self.assertEqual(quote.member_accesses, 0)
                self.assertEqual(store.events(), [])
                state = health.get("fixture")
                self.assertEqual(state.status, "failed")
                self.assertEqual(state.poll_count, 1)
                self.assertEqual(state.total_failures, 1)
                self.assertEqual(state.total_received, 0)
                self.assertEqual(state.total_accepted, 0)
                self.assertEqual(state.total_rejected, 0)
                self.assertIsNone(state.last_success_at)
                self.assertIsNone(state.last_cursor)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
