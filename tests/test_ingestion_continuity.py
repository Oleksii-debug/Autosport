import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.providers import ProviderBatch, ProviderQuote, ProviderUnavailableError
from autosport.source_continuity import (
    ProviderContinuityWitness,
    SourceContinuityStore,
)
from autosport.storage import SQLiteMarketStore


class StaticProvider:
    def __init__(self, source_id: str, batches: list[ProviderBatch]) -> None:
        self.source_id = source_id
        self._batches = list(batches)

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if not self._batches:
            return ProviderBatch(self.source_id, tuple(), cursor="empty")
        return self._batches.pop(0)


class FailingProvider:
    source_id = "source"

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        raise ProviderUnavailableError("bounded outage")


class IngestionContinuityTests(unittest.TestCase):
    @staticmethod
    def _quote(sequence: int) -> ProviderQuote:
        return ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="winner",
            provider_selection_id=f"selection-{sequence}",
            decimal_odds=Decimal("2.0"),
            observed_ts="2026-09-21T08:00:00+00:00",
            sequence=sequence,
            source_ts="2026-09-21T08:00:00+00:00",
        )

    def _runtime(
        self,
        tmp: str,
        *,
        resolver=None,
        clock_start: str = "2026-09-21T08:00:00+00:00",
    ):
        market = SQLiteMarketStore(Path(tmp) / "market.db")
        health = SourceHealthStore(Path(tmp) / "source_health.json")
        bus = MarketEventBus(market)
        start = datetime.fromisoformat(clock_start)
        tick = 0

        def clock() -> str:
            nonlocal tick
            value = start + timedelta(seconds=tick)
            tick += 1
            return value.isoformat()

        engine = IngestionEngine(
            bus,
            policy=IngestionPolicy(
                max_batch_size=100,
                stale_after_seconds=3600,
                max_future_skew_seconds=5,
            ),
            health_store=health,
            continuity_witness_resolver=resolver,
            clock=clock,
        )
        return engine, market, health

    def test_snapshot_health_does_not_imply_continuity(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, market, health = self._runtime(tmp)
            provider = StaticProvider(
                "source",
                [ProviderBatch("source", (self._quote(1),), cursor="snapshot-1")],
            )

            stats = engine.poll_once(provider, max_items=10)

            self.assertEqual(stats.health_status, "healthy")
            self.assertEqual(stats.continuity_status, "unknown")
            self.assertEqual(health.get("source").status, "healthy")
            continuity = SourceContinuityStore(
                Path(tmp) / "source_continuity.json"
            ).get("source")
            self.assertEqual(continuity.status, "unknown")
            self.assertIsNone(continuity.trusted_token)
            market.close()

    def test_resolver_chain_stays_unverified_across_restart(self):
        def resolver(_provider, batch: ProviderBatch):
            mapping = {
                "token-1": ProviderContinuityWitness(None, "token-1"),
                "token-2": ProviderContinuityWitness("token-1", "token-2"),
                "token-3": ProviderContinuityWitness("token-1", "token-3"),
            }
            return mapping[batch.cursor]

        with tempfile.TemporaryDirectory() as tmp:
            engine, market, _health = self._runtime(tmp, resolver=resolver)
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch("source", (self._quote(1),), cursor="token-1"),
                    ProviderBatch("source", (self._quote(2),), cursor="token-2"),
                ],
            )
            first = engine.poll_once(provider, max_items=10)
            second = engine.poll_once(provider, max_items=10)
            self.assertEqual(first.continuity_status, "unknown")
            self.assertEqual(second.continuity_status, "unknown")
            market.close()

            restarted, market2, _health2 = self._runtime(
                tmp,
                resolver=resolver,
                clock_start="2026-09-21T08:02:00+00:00",
            )
            third = restarted.poll_once(
                StaticProvider(
                    "source",
                    [ProviderBatch("source", (self._quote(3),), cursor="token-3")],
                ),
                max_items=10,
            )
            self.assertEqual(third.continuity_status, "unknown")
            self.assertEqual(
                SourceContinuityStore(
                    Path(tmp) / "source_continuity.json"
                ).get("source").trusted_token,
                "token-1",
            )
            market2.close()

    def test_outage_then_snapshot_only_recovery_stays_unknown(self):
        def resolver(_provider, batch: ProviderBatch):
            if batch.cursor == "token-1":
                return ProviderContinuityWitness(None, "token-1")
            if batch.cursor == "token-2":
                return ProviderContinuityWitness("token-1", "token-2")
            return None

        with tempfile.TemporaryDirectory() as tmp:
            engine, market, health = self._runtime(tmp, resolver=resolver)
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch("source", (self._quote(1),), cursor="token-1"),
                    ProviderBatch("source", (self._quote(2),), cursor="token-2"),
                ],
            )
            engine.poll_once(provider, max_items=10)
            self.assertEqual(
                engine.poll_once(provider, max_items=10).continuity_status,
                "unknown",
            )

            with self.assertRaises(ProviderUnavailableError):
                engine.poll_once(FailingProvider(), max_items=10)
            self.assertEqual(health.get("source").status, "failed")
            failed_continuity = SourceContinuityStore(
                Path(tmp) / "source_continuity.json"
            ).get("source")
            self.assertEqual(failed_continuity.status, "unknown")
            self.assertEqual(failed_continuity.trusted_token, "token-1")

            recovered = engine.poll_once(
                StaticProvider(
                    "source",
                    [ProviderBatch("source", (self._quote(3),), cursor="snapshot-3")],
                ),
                max_items=10,
            )
            self.assertEqual(recovered.health_status, "healthy")
            self.assertEqual(recovered.continuity_status, "unknown")
            market.close()

    def test_mismatched_resume_token_keeps_healthy_snapshot_but_fails_continuity_closed(self):
        def resolver(_provider, batch: ProviderBatch):
            mapping = {
                "token-1": ProviderContinuityWitness(None, "token-1"),
                "token-2": ProviderContinuityWitness("token-1", "token-2"),
                "token-99": ProviderContinuityWitness("wrong-token", "token-99"),
            }
            return mapping[batch.cursor]

        with tempfile.TemporaryDirectory() as tmp:
            engine, market, _health = self._runtime(tmp, resolver=resolver)
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch("source", (self._quote(1),), cursor="token-1"),
                    ProviderBatch("source", (self._quote(2),), cursor="token-2"),
                    ProviderBatch("source", (self._quote(3),), cursor="token-99"),
                ],
            )
            engine.poll_once(provider, max_items=10)
            engine.poll_once(provider, max_items=10)
            mismatched = engine.poll_once(provider, max_items=10)

            self.assertEqual(mismatched.health_status, "healthy")
            self.assertEqual(mismatched.continuity_status, "unknown")
            continuity = SourceContinuityStore(
                Path(tmp) / "source_continuity.json"
            ).get("source")
            self.assertEqual(continuity.status, "unknown")
            self.assertEqual(continuity.trusted_token, "token-1")
            self.assertEqual(continuity.reason, "witness_previous_token_mismatch")
            market.close()


if __name__ == "__main__":
    unittest.main()
