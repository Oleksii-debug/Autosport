import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import CommittedIngestionHealthError, IngestionEngine
from autosport.ingestion_health import SourceHealthState
from autosport.market_bus import MarketEventBus
from autosport.providers import ProviderBatch, ProviderQuote, ProviderUnavailableError
from autosport.source_continuity import (
    ProviderContinuityWitness,
    SourceContinuityStore,
)
from autosport.storage import SQLiteMarketStore


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
                    observed_ts="2026-09-21T08:02:00+00:00",
                    sequence=2,
                    source_ts="2026-09-21T08:02:00+00:00",
                ),
            ),
            cursor="token-2",
        )


class FailingProvider:
    source_id = "source"

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        raise ProviderUnavailableError("bounded outage")


class FailingHealthStore:
    def get(self, source_id: str) -> SourceHealthState:
        return SourceHealthState(source_id=source_id)

    def record_success(self, *args, **kwargs):
        raise OSError("health success persistence unavailable")

    def record_failure(self, *args, **kwargs):
        raise OSError("health failure persistence unavailable")


class IngestionContinuityProjectionFailureTests(unittest.TestCase):
    @staticmethod
    def _anchored_store(tmp: str) -> SourceContinuityStore:
        store = SourceContinuityStore(Path(tmp) / "source_continuity.json")
        store.record_success(
            "source",
            now="2026-09-21T08:00:00+00:00",
            cursor="token-1",
            witness=ProviderContinuityWitness(None, "token-1"),
        )
        return store

    def test_health_success_persistence_failure_does_not_skip_continuity_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            continuity = self._anchored_store(tmp)
            market = SQLiteMarketStore(Path(tmp) / "market.db")
            engine = IngestionEngine(
                MarketEventBus(market),
                health_store=FailingHealthStore(),  # type: ignore[arg-type]
                continuity_store=continuity,
                continuity_witness_resolver=lambda _provider, _batch: ProviderContinuityWitness(
                    "token-1", "token-2"
                ),
                clock=lambda: "2026-09-21T08:03:00+00:00",
            )

            with self.assertRaises(CommittedIngestionHealthError):
                engine.poll_once(StaticProvider(), max_items=10)

            state = continuity.get("source")
            self.assertEqual(state.status, "unknown")
            self.assertEqual(state.trusted_token, "token-1")
            self.assertEqual(state.reason, "provider_native_evidence_required")
            market.close()

    def test_health_failure_persistence_error_does_not_skip_continuity_revocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            continuity = self._anchored_store(tmp)
            continuity.record_success(
                "source",
                now="2026-09-21T08:01:00+00:00",
                cursor="token-2",
                witness=ProviderContinuityWitness("token-1", "token-2"),
            )
            market = SQLiteMarketStore(Path(tmp) / "market.db")
            engine = IngestionEngine(
                MarketEventBus(market),
                health_store=FailingHealthStore(),  # type: ignore[arg-type]
                continuity_store=continuity,
                clock=lambda: "2026-09-21T08:02:00+00:00",
            )

            with self.assertRaises(ProviderUnavailableError):
                engine.poll_once(FailingProvider(), max_items=10)

            state = continuity.get("source")
            self.assertEqual(state.status, "unknown")
            self.assertEqual(state.trusted_token, "token-1")
            self.assertEqual(
                state.reason,
                "provider_failure_since_last_continuity_proof",
            )
            market.close()


if __name__ == "__main__":
    unittest.main()
