import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.providers import ProviderBatch, ProviderQuote
from autosport.source_continuity import ProviderContinuityWitness, SourceContinuityStore
from autosport.storage import SQLiteMarketStore


class ForgedContinuityWitness(ProviderContinuityWitness):
    pass


class StaticProvider:
    source_id = "source"

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        return ProviderBatch(
            self.source_id,
            (
                ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="selection-1",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-21T08:00:00+00:00",
                    sequence=1,
                    source_ts="2026-09-21T08:00:00+00:00",
                ),
            ),
            cursor="token-1",
        )


class DuckHealthStore:
    """Existing test doubles need not expose the concrete store's path attribute."""


class IngestionContinuityProvenanceTests(unittest.TestCase):
    def test_nonconcrete_health_store_does_not_create_a_sidecar_from_missing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            market = SQLiteMarketStore(Path(tmp) / "market.db")
            engine = IngestionEngine(
                MarketEventBus(market),
                health_store=DuckHealthStore(),  # type: ignore[arg-type]
            )
            self.assertIsNone(engine.continuity_store)
            market.close()

    def test_forged_witness_is_rejected_before_commit_without_provider_health_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.db"
            health = SourceHealthStore(Path(tmp) / "source_health.json")
            market = SQLiteMarketStore(path)
            engine = IngestionEngine(
                MarketEventBus(market),
                health_store=health,
                continuity_witness_resolver=lambda _provider, _batch: ForgedContinuityWitness(
                    None, "token-1"
                ),
                clock=lambda: "2026-09-21T08:00:01+00:00",
            )

            with self.assertRaisesRegex(
                TypeError,
                "continuity_witness_resolver must return ProviderContinuityWitness or null",
            ):
                engine.poll_once(StaticProvider(), max_items=10)

            health_state = health.get("source")
            self.assertEqual(health_state.status, "unknown")
            self.assertEqual(health_state.poll_count, 0)
            self.assertEqual(health_state.total_failures, 0)

            continuity_state = SourceContinuityStore(
                Path(tmp) / "source_continuity.json"
            ).get("source")
            self.assertEqual(continuity_state.status, "unknown")
            self.assertEqual(continuity_state.reason, "no_continuity_evidence")

            connection = sqlite3.connect(path)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM market_events"
                ).fetchone()[0]
            finally:
                connection.close()
                market.close()
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
