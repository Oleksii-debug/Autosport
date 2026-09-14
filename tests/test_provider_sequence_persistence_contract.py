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


_SQLITE_INT_MIN = -(1 << 63)
_SQLITE_INT_MAX = (1 << 63) - 1


class _SequenceProvider:
    source_id = "fixture"

    def __init__(self, sequence: int) -> None:
        self.sequence = sequence

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        del max_items
        quote = ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="winner",
            provider_selection_id="player-a",
            decimal_odds=Decimal("2.0"),
            observed_ts="2026-09-14T09:15:00+00:00",
            sequence=self.sequence,
        )
        return ProviderBatch(self.source_id, (quote,), cursor="1")


class ProviderSequencePersistenceContractTests(unittest.TestCase):
    def test_provider_quote_rejects_sequence_outside_sqlite_integer_range(self) -> None:
        for sequence in (_SQLITE_INT_MIN - 1, _SQLITE_INT_MAX + 1):
            with self.subTest(sequence=sequence):
                with self.assertRaisesRegex(ValueError, "signed 64-bit SQLite INTEGER"):
                    _SequenceProvider(sequence).read_batch()

    def test_sqlite_integer_boundary_sequences_remain_valid(self) -> None:
        for sequence in (_SQLITE_INT_MIN, _SQLITE_INT_MAX):
            with self.subTest(sequence=sequence):
                batch = _SequenceProvider(sequence).read_batch()
                self.assertEqual(batch.quotes[0].sequence, sequence)

    def test_out_of_range_sequence_fails_before_market_persistence(self) -> None:
        for sequence in (_SQLITE_INT_MIN - 1, _SQLITE_INT_MAX + 1):
            with self.subTest(sequence=sequence), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = SQLiteMarketStore(root / "market.db")
                try:
                    health = SourceHealthStore(root / "source-health.json")
                    engine = IngestionEngine(
                        MarketEventBus(store),
                        health_store=health,
                        clock=lambda: "2026-09-14T09:15:01+00:00",
                    )

                    with self.assertRaisesRegex(ValueError, "signed 64-bit SQLite INTEGER"):
                        engine.poll_once(_SequenceProvider(sequence), max_items=10)

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


if __name__ == "__main__":
    unittest.main()
