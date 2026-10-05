import json
import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine, IngestionStats
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.providers import (
    CanonicalNormalizer,
    InMemoryProvider,
    ProviderBatch,
    ProviderQuote,
    ProviderUnavailableError,
)
from autosport.storage import SQLiteMarketStore


class FailingProvider:
    source_id = "failing-source"

    def read_batch(self, max_items: int = 1000):
        raise RuntimeError("provider unavailable")


class ProviderUnavailableFailingProvider:
    source_id = "provider-unavailable-source"

    def read_batch(self, max_items: int = 1000):
        raise ProviderUnavailableError("provider unavailable")


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
    def test_ingestion_rejects_substituted_provider_identity_and_batch_types(self):
        class Text(str):
            pass

        class Batch(ProviderBatch):
            pass

        class SourceSubclassProvider:
            source_id = Text("source-a")

            def read_batch(self, max_items: int = 1000):
                raise AssertionError("provider read executed")

        class BatchSubclassProvider:
            source_id = "source-a"

            def read_batch(self, max_items: int = 1000):
                return Batch("source-a", ())

        with tempfile.TemporaryDirectory() as tmp:
            engine, store, _health = self._engine(tmp)
            try:
                with self.assertRaisesRegex(TypeError, "source_id must be an exact string"):
                    engine.poll_once(SourceSubclassProvider(), max_items=1)
                with self.assertRaisesRegex(TypeError, "exact ProviderBatch"):
                    engine.poll_once(BatchSubclassProvider(), max_items=1)
                self.assertEqual(store.events(), ())
            finally:
                store.close()

    def test_ingestion_engine_rejects_substituted_authority_components(self):
        class Bus(MarketEventBus):
            pass

        class Normalizer(CanonicalNormalizer):
            pass

        class Policy(IngestionPolicy):
            pass

        class HealthStore(SourceHealthStore):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            health = SourceHealthStore(root / "source-health.json")
            bus = MarketEventBus(store)
            try:
                with self.assertRaisesRegex(TypeError, "exact MarketEventBus"):
                    IngestionEngine(Bus(store))
                with self.assertRaisesRegex(TypeError, "exact CanonicalNormalizer"):
                    IngestionEngine(bus, Normalizer())
                with self.assertRaisesRegex(TypeError, "exact IngestionPolicy"):
                    IngestionEngine(bus, policy=Policy())
                with self.assertRaisesRegex(TypeError, "exact SourceHealthStore"):
                    IngestionEngine(
                        bus,
                        health_store=HealthStore(root / "other-health.json"),
                    )
                with self.assertRaisesRegex(TypeError, "clock must be callable"):
                    IngestionEngine(bus, health_store=health, clock=object())
            finally:
                store.close()

    def test_ingestion_poll_rejects_integer_subclass_batch_bound(self):
        class BatchSize(int):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            engine, store, _health = self._engine(tmp)
            try:
                with self.assertRaisesRegex(ValueError, "max_items"):
                    engine.poll_once(StaticProvider("source", []), max_items=BatchSize(1))
            finally:
                store.close()

    def test_ingestion_policy_rejects_numeric_subclasses(self):
        class BatchSize(int):
            pass

        class Seconds(float):
            pass

        with self.assertRaisesRegex(ValueError, "max_batch_size"):
            IngestionPolicy(max_batch_size=BatchSize(100))
        with self.assertRaisesRegex(ValueError, "stale_after_seconds"):
            IngestionPolicy(stale_after_seconds=Seconds(60.0))
        with self.assertRaisesRegex(ValueError, "max_future_skew_seconds"):
            IngestionPolicy(max_future_skew_seconds=Seconds(5.0))

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

    def test_source_health_rejects_path_rebinding_before_read_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SourceHealthStore(root / "source-health.json")
            canonical = store.path
            store.path = root / "foreign-health.json"

            with self.assertRaisesRegex(
                RuntimeError,
                "persistence authority changed",
            ):
                store.get("source-a")
            with self.assertRaisesRegex(
                RuntimeError,
                "persistence authority changed",
            ):
                store.record_failure(
                    "source-a",
                    now="2026-09-12T12:00:00+00:00",
                    error=RuntimeError("offline"),
                )

            self.assertTrue(canonical.exists())
            self.assertFalse((root / "foreign-health.json").exists())

    def test_source_health_rejects_lock_namespace_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SourceHealthStore(root / "source-health.json")
            store._lock_path = root / "foreign.lock"

            with self.assertRaisesRegex(
                RuntimeError,
                "persistence authority changed",
            ):
                store.record_failure(
                    "source-a",
                    now="2026-09-12T12:00:00+00:00",
                    error=RuntimeError("offline"),
                )

            self.assertFalse((root / "foreign.lock").exists())

    def test_source_health_rejects_preexisting_temporary_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SourceHealthStore(root / "source-health.json")
            target = root / "foreign-target.json"
            target.write_text("sentinel", encoding="utf-8")
            temporary = store._temporary_path_authority
            try:
                temporary.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("file symlinks are unavailable")

            with self.assertRaisesRegex(
                RuntimeError,
                "temporary persistence path already exists",
            ):
                store.record_failure(
                    "source-a",
                    now="2026-09-12T12:00:00+00:00",
                    error=RuntimeError("offline"),
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")
            self.assertTrue(temporary.is_symlink())

    def test_source_health_rejects_preexisting_temporary_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SourceHealthStore(root / "source-health.json")
            temporary = store._temporary_path_authority
            temporary.write_text("stale", encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "temporary persistence path already exists",
            ):
                store.record_failure(
                    "source-a",
                    now="2026-09-12T12:00:00+00:00",
                    error=RuntimeError("offline"),
                )

            self.assertEqual(temporary.read_text(encoding="utf-8"), "stale")

    def test_source_health_rejects_writer_lock_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SourceHealthStore(root / "source-health.json")
            store._lock_path.unlink(missing_ok=True)
            target = root / "foreign-lock-target"
            target.write_text("sentinel", encoding="utf-8")
            try:
                store._lock_path.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("file symlinks are unavailable")

            with self.assertRaisesRegex(
                RuntimeError,
                "writer-lock path is unavailable|regular file",
            ):
                store.record_failure(
                    "source-a",
                    now="2026-09-12T12:00:00+00:00",
                    error=RuntimeError("offline"),
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")

    def test_source_health_rejects_hardlinked_target_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "source-health.json"
            store = SourceHealthStore(path)
            alias = root / "source-health-alias.json"
            try:
                os.link(path, alias)
            except OSError:
                self.skipTest("hardlinks are unavailable")

            with self.assertRaisesRegex(
                RuntimeError,
                "one regular non-symlink file",
            ):
                store.get("source-a")

    def test_source_health_rejects_hardlinked_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SourceHealthStore(root / "source-health.json")
            alias = root / "source-health-lock-alias"
            try:
                os.link(store._lock_path, alias)
            except OSError:
                self.skipTest("hardlinks are unavailable")

            with self.assertRaisesRegex(
                RuntimeError,
                "writer-lock path must be one regular file",
            ):
                store.record_failure(
                    "source-a",
                    now="2026-09-12T12:00:00+00:00",
                    error=RuntimeError("offline"),
                )

    def test_source_health_relative_path_survives_cwd_change(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            try:
                os.chdir(first)
                store = SourceHealthStore("source-health.json")
                canonical = store.path
                os.chdir(second)
                state = store.record_failure(
                    "source-a",
                    now="2026-09-12T12:00:00+00:00",
                    error=RuntimeError("offline"),
                )
                self.assertEqual(state.status, "failed")
                self.assertEqual(store.path, canonical)
                self.assertTrue(canonical.exists())
                self.assertFalse((second / "source-health.json").exists())
            finally:
                os.chdir(original_cwd)

    def test_source_health_symlinked_parent_aliases_share_lock_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            alias = root / "alias"
            real.mkdir()
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")

            first = SourceHealthStore(real / "source-health.json")
            second = SourceHealthStore(alias / "source-health.json")
            self.assertEqual(first.path, second.path)
            self.assertEqual(first._lock_path, second._lock_path)

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

    def test_provider_source_identity_mutation_during_read_fails_before_market_persistence(self):
        class MutatingProvider:
            def __init__(self) -> None:
                self.source_id = "source"

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                self.source_id = "mutated-source"
                return ProviderBatch(
                    "source",
                    (IngestionHealthTests._quote(None),),
                    cursor="cursor-1",
                )

        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            with self.assertRaisesRegex(
                ValueError,
                "source_id changed during batch acquisition",
            ):
                engine.poll_once(MutatingProvider(), max_items=10)

            self.assertEqual(store.events(), ())
            state = health.get("source")
            self.assertEqual(state.status, "failed")
            self.assertEqual(state.last_failure_kind, "provider_or_validation")
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

    def test_source_health_locked_success_adds_regression_flag_against_current_high_water(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = SourceHealthStore(Path(tmp) / "source-health.json")
            health.record_success(
                "source",
                now="2026-09-12T12:00:00+00:00",
                received=1,
                accepted=1,
                rejected=0,
                cursor="high",
                latest_source_ts="2026-09-12T11:59:59+00:00",
                quality_flags=(),
            )

            state = health.record_success(
                "source",
                now="2026-09-12T12:00:01+00:00",
                received=1,
                accepted=1,
                rejected=0,
                cursor="regressed",
                latest_source_ts="2026-09-12T11:59:58+00:00",
                quality_flags=(),
            )

            self.assertEqual(state.latest_source_ts, "2026-09-12T11:59:59+00:00")
            self.assertEqual(state.status, "degraded")
            self.assertIn("SOURCE_TIME_REGRESSION", state.quality_flags)

    def test_ingestion_stats_reflect_health_locked_regression_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            provider = StaticProvider(
                "source",
                [
                    ProviderBatch(
                        "source",
                        (self._quote("2026-09-12T11:59:58+00:00"),),
                        cursor="poll",
                    )
                ],
            )
            original_publish = engine.bus.publish_many

            def publish_then_advance_health(events):
                accepted = original_publish(events)
                health.record_success(
                    "source",
                    now="2026-09-12T12:00:00+00:00",
                    received=0,
                    accepted=0,
                    rejected=0,
                    cursor="peer",
                    latest_source_ts="2026-09-12T11:59:59+00:00",
                    quality_flags=(),
                )
                return accepted

            with patch.object(
                engine.bus,
                "publish_many",
                side_effect=publish_then_advance_health,
            ):
                stats = engine.poll_once(provider, max_items=10)

            self.assertIn("SOURCE_TIME_REGRESSION", stats.quality_flags)
            self.assertEqual(stats.health_status, "degraded")
            self.assertEqual(
                health.get("source").latest_source_ts,
                "2026-09-12T11:59:59+00:00",
            )
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
            self.assertEqual(state.last_failure_kind, "provider_or_validation")
            self.assertEqual(state.consecutive_failure_kind_count, 1)
            self.assertIn("RuntimeError", state.last_error or "")
            store.close()

    def test_provider_unavailable_is_typed_at_ingestion_failure_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, health = self._engine(tmp)
            with self.assertRaises(ProviderUnavailableError):
                engine.poll_once(ProviderUnavailableFailingProvider(), max_items=10)
            state = health.get("provider-unavailable-source")
            self.assertEqual(state.status, "failed")
            self.assertEqual(state.consecutive_failures, 1)
            self.assertEqual(state.last_failure_kind, "provider_unavailable")
            self.assertEqual(state.consecutive_failure_kind_count, 1)
            store.close()

    def test_typed_failure_suffix_resets_on_kind_change_and_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            health = SourceHealthStore(path)
            source_id = "typed-source"

            health.record_failure(
                source_id,
                now="2026-09-12T12:00:00+00:00",
                error=ProviderUnavailableError("outage-1"),
                failure_kind="provider_unavailable",
            )
            same_kind = health.record_failure(
                source_id,
                now="2026-09-12T12:00:01+00:00",
                error=ProviderUnavailableError("outage-2"),
                failure_kind="provider_unavailable",
            )
            self.assertEqual(same_kind.consecutive_failures, 2)
            self.assertEqual(same_kind.consecutive_failure_kind_count, 2)

            changed_kind = health.record_failure(
                source_id,
                now="2026-09-12T12:00:02+00:00",
                error=ValueError("invalid payload"),
                failure_kind="provider_or_validation",
            )
            self.assertEqual(changed_kind.consecutive_failures, 3)
            self.assertEqual(changed_kind.last_failure_kind, "provider_or_validation")
            self.assertEqual(changed_kind.consecutive_failure_kind_count, 1)

            recovered = health.record_success(
                source_id,
                now="2026-09-12T12:00:03+00:00",
                received=0,
                accepted=0,
                rejected=0,
                cursor="recovered",
                latest_source_ts=None,
                quality_flags=(),
            )
            self.assertEqual(recovered.consecutive_failures, 0)
            self.assertIsNone(recovered.last_failure_kind)
            self.assertEqual(recovered.consecutive_failure_kind_count, 0)

    def test_schema_v3_failure_remains_untyped_until_new_causal_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            health = SourceHealthStore(path)
            health.record_failure(
                "legacy-source",
                now="2026-09-12T12:00:00+00:00",
                error=RuntimeError("legacy failure"),
            )

            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["schema_version"] = 3
            for payload in raw["sources"].values():
                payload.pop("last_failure_kind")
                payload.pop("consecutive_failure_kind_count")
            for entries in raw["history"].values():
                for entry in entries:
                    entry["state"].pop("last_failure_kind")
                    entry["state"].pop("consecutive_failure_kind_count")
            path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            reopened = SourceHealthStore(path)
            legacy = reopened.get("legacy-source")
            self.assertEqual(legacy.status, "failed")
            self.assertIsNone(legacy.last_failure_kind)
            self.assertEqual(legacy.consecutive_failure_kind_count, 0)

            typed = reopened.record_failure(
                "legacy-source",
                now="2026-09-12T12:00:01+00:00",
                error=ProviderUnavailableError("new typed outage"),
                failure_kind="provider_unavailable",
            )
            self.assertEqual(typed.consecutive_failures, 2)
            self.assertEqual(typed.last_failure_kind, "provider_unavailable")
            self.assertEqual(typed.consecutive_failure_kind_count, 1)
            upgraded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["schema_version"], 4)

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


    def test_future_local_observation_is_rejected_before_market_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:00+00:00",
                )
                provider = StaticProvider(
                    "source",
                    [
                        ProviderBatch(
                            "source",
                            (
                                ProviderQuote(
                                    provider_event_id="event-1",
                                    provider_market_id="winner",
                                    provider_selection_id="future-local",
                                    decimal_odds=Decimal("2.0"),
                                    observed_ts="2026-09-12T12:00:01+00:00",
                                    sequence=1,
                                    source_ts="2026-09-12T11:59:59+00:00",
                                ),
                            ),
                            cursor="future-local",
                        )
                    ],
                )

                stats = engine.poll_once(provider, max_items=10)

                self.assertEqual(stats.accepted, 0)
                self.assertEqual(stats.rejected, 1)
                self.assertEqual(
                    stats.quality_flags,
                    ("FUTURE_OBSERVATION_TIMESTAMP",),
                )
                self.assertEqual(store.events(), [])
                state = health.get("source")
                self.assertEqual(state.status, "degraded")
                self.assertEqual(state.total_received, 1)
                self.assertEqual(state.total_accepted, 0)
                self.assertEqual(state.total_rejected, 1)
                self.assertEqual(
                    state.quality_flags,
                    ("FUTURE_OBSERVATION_TIMESTAMP",),
                )
            finally:
                store.close()

    def test_future_local_observation_does_not_reject_valid_sibling_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:00+00:00",
                )
                valid = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="valid",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-12T12:00:00+00:00",
                    sequence=1,
                    source_ts="2026-09-12T11:59:59+00:00",
                )
                future = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="future",
                    decimal_odds=Decimal("2.1"),
                    observed_ts="2026-09-12T12:00:00.000001+00:00",
                    sequence=2,
                    source_ts="2026-09-12T11:59:59+00:00",
                )
                provider = StaticProvider(
                    "source",
                    [ProviderBatch("source", (valid, future), cursor="mixed")],
                )

                stats = engine.poll_once(provider, max_items=10)

                self.assertEqual(stats.accepted, 1)
                self.assertEqual(stats.rejected, 1)
                self.assertIn("FUTURE_OBSERVATION_TIMESTAMP", stats.quality_flags)
                persisted = store.events()
                self.assertEqual(len(persisted), 1)
                self.assertEqual(persisted[0].selection_id, "source:valid")
            finally:
                store.close()


    def test_submicrosecond_local_observation_is_quote_local_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:01+00:00",
                )
                valid = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="valid",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-12T12:00:00+00:00",
                    sequence=1,
                )
                unsupported = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="submicro",
                    decimal_odds=Decimal("2.1"),
                    observed_ts="2026-09-12T12:00:00.0000001+00:00",
                    sequence=2,
                )
                provider = StaticProvider(
                    "source",
                    [ProviderBatch("source", (valid, unsupported), cursor="precision")],
                )

                stats = engine.poll_once(provider, max_items=10)

                self.assertEqual(stats.accepted, 1)
                self.assertEqual(stats.rejected, 1)
                self.assertIn("INVALID_QUOTE", stats.quality_flags)
                persisted = store.events()
                self.assertEqual(len(persisted), 1)
                self.assertEqual(persisted[0].selection_id, "source:valid")
                self.assertEqual(health.get("source").status, "degraded")
            finally:
                store.close()

    def test_zero_only_submicrosecond_receipt_tail_remains_exactly_admissible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:01+00:00",
                )
                quote = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="exact-zero-tail",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-12T12:00:00.123456000+00:00",
                    sequence=1,
                )
                provider = StaticProvider(
                    "source",
                    [ProviderBatch("source", (quote,), cursor="zero-tail")],
                )

                stats = engine.poll_once(provider, max_items=10)

                self.assertEqual(stats.accepted, 1)
                self.assertEqual(stats.rejected, 0)
                self.assertEqual(stats.quality_flags, ())
                persisted = store.events()
                self.assertEqual(len(persisted), 1)
                self.assertEqual(
                    persisted[0].observed_ts,
                    "2026-09-12T12:00:00.123456000+00:00",
                )
            finally:
                store.close()


    def test_submicrosecond_source_time_is_rejected_before_truth_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    policy=IngestionPolicy(
                        max_batch_size=10,
                        stale_after_seconds=60,
                        max_future_skew_seconds=5,
                    ),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:00+00:00",
                )
                quote = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="submicro-source",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-12T12:00:00+00:00",
                    sequence=1,
                    source_ts="2026-09-12T12:00:05.0000001+00:00",
                )
                provider = StaticProvider(
                    "source",
                    [ProviderBatch("source", (quote,), cursor="source-precision")],
                )

                stats = engine.poll_once(provider, max_items=10)

                self.assertEqual(stats.accepted, 0)
                self.assertEqual(stats.rejected, 1)
                self.assertEqual(stats.quality_flags, ("INVALID_SOURCE_TIMESTAMP",))
                self.assertEqual(store.events(), [])
                state = health.get("source")
                self.assertEqual(state.status, "degraded")
                self.assertIsNone(state.latest_source_ts)
            finally:
                store.close()

    def test_zero_only_source_precision_tail_preserves_source_high_water(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:01+00:00",
                )
                quote = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="zero-tail-source",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-12T12:00:00+00:00",
                    sequence=1,
                    source_ts="2026-09-12T11:59:59.123456000+00:00",
                )
                provider = StaticProvider(
                    "source",
                    [ProviderBatch("source", (quote,), cursor="source-zero-tail")],
                )

                stats = engine.poll_once(provider, max_items=10)

                self.assertEqual(stats.accepted, 1)
                self.assertEqual(stats.rejected, 0)
                self.assertNotIn("INVALID_SOURCE_TIMESTAMP", stats.quality_flags)
                state = health.get("source")
                self.assertEqual(
                    state.latest_source_ts,
                    "2026-09-12T11:59:59.123456+00:00",
                )
            finally:
                store.close()


    def test_source_health_rejects_nonzero_submicrosecond_transition_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = SourceHealthStore(Path(tmp) / "source-health.json")

            with self.assertRaisesRegex(
                ValueError,
                "precision finer than microseconds is unsupported",
            ):
                health.record_success(
                    "source",
                    now="2026-09-12T12:00:00.0000001+00:00",
                    received=0,
                    accepted=0,
                    rejected=0,
                    cursor="submicro-now",
                    latest_source_ts=None,
                    quality_flags=(),
                )

            state = health.get("source")
            self.assertEqual(state.status, "unknown")
            self.assertEqual(state.poll_count, 0)

    def test_source_health_accepts_zero_only_precision_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = SourceHealthStore(Path(tmp) / "source-health.json")

            state = health.record_success(
                "source",
                now="2026-09-12T12:00:00.123456000+00:00",
                received=0,
                accepted=0,
                rejected=0,
                cursor="zero-tail-now",
                latest_source_ts=None,
                quality_flags=(),
            )

            self.assertEqual(
                state.last_success_at,
                "2026-09-12T12:00:00.123456000+00:00",
            )
            as_of = health.get_as_of(
                "source",
                as_of=datetime.fromisoformat(
                    "2026-09-12T12:00:00.123456+00:00"
                ),
            )
            self.assertEqual(as_of.poll_count, 1)

    def test_ingestion_clock_submicrosecond_precision_fails_before_market_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:00.0000001+00:00",
                )
                provider = StaticProvider(
                    "source",
                    [
                        ProviderBatch(
                            "source",
                            (
                                ProviderQuote(
                                    provider_event_id="event-1",
                                    provider_market_id="winner",
                                    provider_selection_id="selection",
                                    decimal_odds=Decimal("2.0"),
                                    observed_ts="2026-09-12T12:00:00+00:00",
                                    sequence=1,
                                ),
                            ),
                        )
                    ],
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "precision finer than microseconds is unsupported",
                ):
                    engine.poll_once(provider, max_items=10)

                self.assertEqual(store.events(), [])
                self.assertEqual(health.get("source").poll_count, 0)
            finally:
                store.close()


    def test_observed_time_is_freshness_fallback_when_source_time_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    policy=IngestionPolicy(
                        max_batch_size=10,
                        stale_after_seconds=60,
                        max_future_skew_seconds=5,
                    ),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:00+00:00",
                )
                stale = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="stale-observed",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-12T11:58:59+00:00",
                    sequence=1,
                    source_ts=None,
                )
                provider = StaticProvider(
                    "source",
                    [ProviderBatch("source", (stale,), cursor="stale-observed")],
                )

                stats = engine.poll_once(provider, max_items=10)

                self.assertEqual(stats.accepted, 1)
                self.assertEqual(stats.rejected, 0)
                self.assertIn("STALE_SOURCE", stats.quality_flags)
                state = health.get("source")
                self.assertEqual(state.status, "degraded")
                self.assertIn("STALE_SOURCE", state.quality_flags)
                self.assertIsNone(state.latest_source_ts)
            finally:
                store.close()

    def test_fresh_observed_fallback_without_source_time_remains_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    policy=IngestionPolicy(
                        max_batch_size=10,
                        stale_after_seconds=60,
                        max_future_skew_seconds=5,
                    ),
                    health_store=health,
                    clock=lambda: "2026-09-12T12:00:00+00:00",
                )
                fresh = ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="fresh-observed",
                    decimal_odds=Decimal("2.0"),
                    observed_ts="2026-09-12T11:59:30+00:00",
                    sequence=1,
                    source_ts=None,
                )
                provider = StaticProvider(
                    "source",
                    [ProviderBatch("source", (fresh,), cursor="fresh-observed")],
                )

                stats = engine.poll_once(provider, max_items=10)

                self.assertEqual(stats.accepted, 1)
                self.assertEqual(stats.quality_flags, ())
                self.assertEqual(stats.health_status, "healthy")
                self.assertEqual(health.get("source").status, "healthy")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
