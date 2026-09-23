import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine, IngestionStats
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.providers import InMemoryProvider, ProviderBatch, ProviderQuote
from autosport.storage import SQLiteMarketStore


class FailingProvider:
    source_id = "failing-source"

    def read_batch(self, max_items: int = 1000):
        raise RuntimeError("provider unavailable")


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


class IngestionHealthTests(unittest.TestCase):
    def _engine(self, tmp: str, *, now: str = "2026-09-12T12:00:00+00:00", policy=None):
        store = SQLiteMarketStore(Path(tmp) / "market.db")
        bus = MarketEventBus(store)
        health = SourceHealthStore(Path(tmp) / "source-health.json")
        clock_point = datetime.fromisoformat(now)
        clock_tick = 0

        def clock() -> str:
            nonlocal clock_tick
            point = clock_point + timedelta(microseconds=clock_tick)
            clock_tick += 1
            return point.isoformat()

        engine = IngestionEngine(
            bus,
            policy=policy or IngestionPolicy(max_batch_size=100, stale_after_seconds=60, max_future_skew_seconds=5),
            health_store=health,
            clock=clock,
        )
        return engine, store, health

    @staticmethod
    def _quote(source_ts: str | None, sequence: int = 1, selection: str = "a") -> ProviderQuote:
        return ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="winner",
            provider_selection_id=selection,
            decimal_odds=Decimal("2.0"),
            observed_ts="2026-09-12T12:00:00+00:00",
            sequence=sequence,
            source_ts=source_ts,
        )

    def test_stats_throughput_uses_finite_positive_elapsed(self):
        stats = IngestionStats(
            source_id="source",
            received=2,
            accepted=1,
            rejected=1,
            elapsed_seconds=0.25,
            cursor=None,
        )

        self.assertEqual(stats.accepted_per_second, 4.0)

    def test_stats_throughput_rejects_invalid_elapsed_truth(self):
        invalid_values = (True, "1", 0.0, -1.0, float("nan"), float("inf"), float("-inf"))
        for elapsed in invalid_values:
            with self.subTest(elapsed=elapsed):
                stats = IngestionStats(
                    source_id="source",
                    received=1,
                    accepted=1,
                    rejected=0,
                    elapsed_seconds=elapsed,  # type: ignore[arg-type]
                    cursor=None,
                )
                with self.assertRaisesRegex(ValueError, "elapsed_seconds must be a finite positive number"):
                    _ = stats.accepted_per_second

    def test_policy_rejects_invalid_backpressure_bound(self):
        for value in (True, 1.5, float("nan"), float("inf")):
            with self.subTest(max_batch_size=value):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    IngestionPolicy(max_batch_size=value)

    def test_policy_rejects_nonfinite_or_nonnumeric_truth_thresholds(self):
        invalid_values = (True, float("nan"), float("inf"), float("-inf"), "60")
        for field_name in ("stale_after_seconds", "max_future_skew_seconds"):
            for value in invalid_values:
                with self.subTest(field_name=field_name, value=value):
                    kwargs = {field_name: value}
                    with self.assertRaisesRegex(ValueError, "finite non-negative number"):
                        IngestionPolicy(**kwargs)

    def test_policy_allows_zero_truth_thresholds(self):
        policy = IngestionPolicy(max_batch_size=1, stale_after_seconds=0, max_future_skew_seconds=0)
        self.assertEqual(policy.stale_after_seconds, 0)
        self.assertEqual(policy.max_future_skew_seconds, 0)

    def test_request_bound_rejects_invalid_values_before_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, _health = self._engine(tmp)
            provider = StaticProvider("source", [ProviderBatch("source", tuple())])
            for value in (True, 1.5, float("nan"), float("inf")):
                with self.subTest(max_items=value):
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        engine.poll_once(provider, max_items=value)
            self.assertEqual(provider.calls, 0)
            store.close()

    def test_backpressure_limit_rejects_oversized_request_before_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, _health = self._engine(
                tmp, policy=IngestionPolicy(max_batch_size=10, stale_after_seconds=60, max_future_skew_seconds=5)
            )
            provider = StaticProvider("source", [ProviderBatch("source", tuple())])
            with self.assertRaisesRegex(ValueError, "backpressure limit"):
                engine.poll_once(provider, max_items=11)
            self.assertEqual(provider.calls, 0)
            store.close()

    def test_provider_cannot_return_more_than_requested_batch_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            provider = StaticProvider(
                "source",
                [ProviderBatch("source", (self._quote(None, 1, "a"), self._quote(None, 2, "b")))],
            )
            with self.assertRaisesRegex(ValueError, "above requested batch bound"):
                engine.poll_once(provider, max_items=1)
            self.assertEqual(len(store.events()), 0)
            self.assertEqual(health.get("source").status, "failed")
            store.close()

    def test_success_persists_health_counters_and_provider_gap_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch(
                        "source",
                        (self._quote("2026-09-12T11:59:50+00:00"),),
                        cursor="cursor-1",
                        quality_flags=("PROVIDER_SEQUENCE_GAP",),
                    )
                ],
            )
            stats = engine.poll_once(provider, max_items=10)
            self.assertEqual(stats.accepted, 1)
            self.assertEqual(stats.health_status, "degraded")
            self.assertEqual(stats.quality_flags, ("PROVIDER_SEQUENCE_GAP",))
            reopened = SourceHealthStore(Path(tmp) / "source-health.json").get("source")
            self.assertEqual(reopened.poll_count, 1)
            self.assertEqual(reopened.total_received, 1)
            self.assertEqual(reopened.total_accepted, 1)
            self.assertEqual(reopened.consecutive_failures, 0)
            self.assertEqual(reopened.last_cursor, "cursor-1")
            store.close()

    def test_normalization_rejection_degrades_source_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            invalid_quote = ProviderQuote(
                provider_event_id="event-1",
                provider_market_id="winner",
                provider_selection_id="invalid-odds",
                decimal_odds=Decimal("1.0"),
                observed_ts="2026-09-12T12:00:00+00:00",
                sequence=1,
            )
            provider = StaticProvider(
                "source",
                [ProviderBatch("source", (invalid_quote,), cursor="cursor-invalid")],
            )

            stats = engine.poll_once(provider, max_items=10)

            self.assertEqual(stats.received, 1)
            self.assertEqual(stats.accepted, 0)
            self.assertEqual(stats.rejected, 1)
            self.assertEqual(stats.quality_flags, ("INVALID_QUOTE",))
            self.assertEqual(stats.health_status, "degraded")
            self.assertEqual(len(store.events()), 0)
            persisted = health.get("source")
            self.assertEqual(persisted.status, "degraded")
            self.assertEqual(persisted.total_received, 1)
            self.assertEqual(persisted.total_accepted, 0)
            self.assertEqual(persisted.total_rejected, 1)
            self.assertEqual(persisted.quality_flags, ("INVALID_QUOTE",))
            self.assertEqual(persisted.last_cursor, "cursor-invalid")
            store.close()

    def test_stale_future_skew_and_invalid_source_time_are_truth_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch(
                        "source",
                        (
                            self._quote("2026-09-12T11:57:00+00:00", 1, "stale"),
                            self._quote("2026-09-12T12:00:10+00:00", 2, "future"),
                            self._quote("not-a-timestamp", 3, "invalid"),
                        ),
                    )
                ],
            )
            stats = engine.poll_once(provider, max_items=10)
            self.assertEqual(stats.accepted, 2)
            self.assertEqual(stats.rejected, 1)
            self.assertEqual(
                set(stats.quality_flags),
                {"STALE_SOURCE", "FUTURE_CLOCK_SKEW", "INVALID_SOURCE_TIMESTAMP"},
            )
            self.assertEqual(len(store.events()), 2)
            self.assertEqual(health.get("source").status, "degraded")
            store.close()

    def test_source_time_regression_is_detected_without_lowering_high_water_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch("source", (self._quote("2026-09-12T11:59:50+00:00", 10, "a"),), cursor="1"),
                    ProviderBatch("source", (self._quote("2026-09-12T11:59:40+00:00", 11, "b"),), cursor="2"),
                ],
            )
            first = engine.poll_once(provider, max_items=10)
            second = engine.poll_once(provider, max_items=10)
            self.assertNotIn("SOURCE_TIME_REGRESSION", first.quality_flags)
            self.assertIn("SOURCE_TIME_REGRESSION", second.quality_flags)
            state = health.get("source")
            self.assertEqual(state.poll_count, 2)
            self.assertEqual(state.status, "degraded")
            self.assertEqual(state.latest_source_ts, "2026-09-12T11:59:50+00:00")
            store.close()

    def test_provider_failure_is_persisted_and_rethrown(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                engine.poll_once(FailingProvider(), max_items=10)
            state = health.get("failing-source")
            self.assertEqual(state.status, "failed")
            self.assertEqual(state.total_failures, 1)
            self.assertEqual(state.consecutive_failures, 1)
            self.assertIn("RuntimeError", state.last_error or "")
            store.close()

    def test_duplicate_batch_flags_fail_at_provider_contract_boundary(self):
        with self.assertRaisesRegex(ValueError, "duplicate provider batch quality flag"):
            ProviderBatch("source", tuple(), quality_flags=("GAP", "GAP"))

    def test_existing_inmemory_provider_remains_backward_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, _health = self._engine(tmp)
            provider = InMemoryProvider("fixture", [self._quote(None)])
            stats = engine.poll_once(provider, max_items=10)
            self.assertEqual(stats.accepted, 1)
            self.assertEqual(stats.health_status, "healthy")
            store.close()


if __name__ == "__main__":
    unittest.main()
