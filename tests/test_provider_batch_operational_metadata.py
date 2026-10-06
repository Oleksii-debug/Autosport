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


class MalformedBatchProvider:
    source_id = "fixture"

    def __init__(self, *, cursor: object = None, quality_flags: object = ()) -> None:
        self.cursor = cursor
        self.quality_flags = quality_flags

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        quote = ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="winner",
            provider_selection_id="player-a",
            decimal_odds=Decimal("2.0"),
            observed_ts="2026-09-14T08:00:00+00:00",
            sequence=1,
        )
        return ProviderBatch(
            self.source_id,
            (quote,),
            cursor=self.cursor,  # type: ignore[arg-type]
            quality_flags=self.quality_flags,  # type: ignore[arg-type]
        )


class ProviderBatchOperationalMetadataTests(unittest.TestCase):
    def test_malformed_operational_metadata_fails_before_event_persistence(self) -> None:
        cases: tuple[tuple[str, object, object, type[BaseException], str], ...] = (
            ("wrong cursor type", 7, (), TypeError, "cursor must be str or None"),
            ("quality flags not tuple", None, ["GAP"], TypeError, "must be a tuple"),
            ("quality flag not string", None, (7,), TypeError, "quality flag must be str"),
            ("blank quality flag", None, ("",), ValueError, "non-empty and trimmed"),
            ("untrimmed quality flag", None, (" GAP",), ValueError, "non-empty and trimmed"),
        )

        for label, cursor, quality_flags, error_type, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                store = SQLiteMarketStore(Path(tmp) / "market.db")
                try:
                    health = SourceHealthStore(Path(tmp) / "source-health.json")
                    engine = IngestionEngine(
                        MarketEventBus(store),
                        health_store=health,
                        clock=lambda: "2026-09-14T08:00:01+00:00",
                    )
                    provider = MalformedBatchProvider(
                        cursor=cursor,
                        quality_flags=quality_flags,
                    )

                    with self.assertRaisesRegex(error_type, message):
                        engine.poll_once(provider, max_items=10)

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

    def test_valid_operational_metadata_remains_accepted(self) -> None:
        batch = ProviderBatch(
            "fixture",
            tuple(),
            cursor="",
            quality_flags=("PROVIDER_SEQUENCE_GAP",),
        )

        self.assertEqual(batch.cursor, "")
        self.assertEqual(batch.quality_flags, ("PROVIDER_SEQUENCE_GAP",))


if __name__ == "__main__":
    unittest.main()
