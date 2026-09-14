import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from autosport.ingestion import CommittedIngestionHealthError, IngestionEngine
from autosport.ingestion_health import (
    SourceHealthState,
    SourceHealthStore,
    _SourceHealthWriterLock,
)
from autosport.market_bus import MarketEventDeliveryError
from autosport.providers import ProviderBatch, ProviderQuote


class _SuccessfulProvider:
    source_id = "source"

    def __init__(self, batch: ProviderBatch) -> None:
        self.batch = batch
        self.calls = 0

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        self.calls += 1
        return self.batch


class _TrackingHealthStore:
    def __init__(self, *, fail_get: bool = False, fail_success: bool = False) -> None:
        self.fail_get = fail_get
        self.fail_success = fail_success
        self.failure_calls = 0
        self.success_calls = 0
        self.last_success: dict[str, object] | None = None

    def get(self, source_id: str):
        if self.fail_get:
            raise RuntimeError("local health read failed")
        return SourceHealthState(source_id=source_id)

    def record_failure(self, source_id: str, *, now: str, error: BaseException):
        self.failure_calls += 1
        return None

    def record_success(self, source_id: str, **kwargs):
        self.success_calls += 1
        self.last_success = {"source_id": source_id, **kwargs}
        if self.fail_success:
            raise OSError("health write failed")
        quality_flags = tuple(kwargs["quality_flags"])
        return SimpleNamespace(status="degraded" if quality_flags else "healthy")


class _UnusedBus:
    def publish_many(self, events):
        raise AssertionError("local failure must occur before persistence")


class _CommittedBus:
    def __init__(self) -> None:
        self.calls = 0
        self.events = ()

    def publish_many(self, events):
        self.calls += 1
        self.events = tuple(events)
        return len(self.events)


class _CommittedDeliveryFailingBus(_CommittedBus):
    def publish_many(self, events):
        accepted = tuple(events)
        self.calls += 1
        self.events = accepted
        raise MarketEventDeliveryError(
            "subscriber failed after persistence",
            [RuntimeError("subscriber failed")],
            accepted,
        )


class _ExplodingNormalizer:
    def normalize(self, source_id: str, quote: ProviderQuote):
        raise RuntimeError("local normalizer failed")


class IngestionFailureAttributionTests(unittest.TestCase):
    @staticmethod
    def _quote() -> ProviderQuote:
        return ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="winner",
            provider_selection_id="a",
            decimal_odds=Decimal("2.0"),
            observed_ts="2026-09-14T08:00:00+00:00",
            sequence=1,
        )

    def test_local_health_read_failure_is_not_recorded_as_provider_failure(self):
        health = _TrackingHealthStore(fail_get=True)
        provider = _SuccessfulProvider(ProviderBatch("source", tuple()))
        engine = IngestionEngine(
            _UnusedBus(),
            health_store=health,  # type: ignore[arg-type]
            clock=lambda: "2026-09-14T08:00:00+00:00",
        )

        with self.assertRaisesRegex(RuntimeError, "local health read failed"):
            engine.poll_once(provider, max_items=10)

        self.assertEqual(provider.calls, 1)
        self.assertEqual(health.failure_calls, 0)

    def test_local_clock_validation_failure_is_not_recorded_as_provider_failure(self):
        health = _TrackingHealthStore()
        provider = _SuccessfulProvider(ProviderBatch("source", tuple()))
        engine = IngestionEngine(
            _UnusedBus(),
            health_store=health,  # type: ignore[arg-type]
            clock=lambda: "not-a-local-clock-timestamp",
        )

        with self.assertRaisesRegex(ValueError, "invalid provider source timestamp"):
            engine.poll_once(provider, max_items=10)

        self.assertEqual(provider.calls, 1)
        self.assertEqual(health.failure_calls, 0)

    def test_unexpected_local_normalizer_failure_is_not_recorded_as_provider_failure(self):
        health = _TrackingHealthStore()
        provider = _SuccessfulProvider(ProviderBatch("source", (self._quote(),)))
        engine = IngestionEngine(
            _UnusedBus(),
            normalizer=_ExplodingNormalizer(),  # type: ignore[arg-type]
            health_store=health,  # type: ignore[arg-type]
            clock=lambda: "2026-09-14T08:00:00+00:00",
        )

        with self.assertRaisesRegex(RuntimeError, "local normalizer failed"):
            engine.poll_once(provider, max_items=10)

        self.assertEqual(provider.calls, 1)
        self.assertEqual(health.failure_calls, 0)

    def test_post_commit_health_failure_preserves_exact_outcome_without_republishing(self):
        health = _TrackingHealthStore(fail_success=True)
        bus = _CommittedBus()
        provider = _SuccessfulProvider(ProviderBatch("source", (self._quote(),), cursor="cursor-1"))
        engine = IngestionEngine(
            bus,  # type: ignore[arg-type]
            health_store=health,  # type: ignore[arg-type]
            clock=lambda: "2026-09-14T08:00:01+00:00",
        )

        with self.assertRaises(CommittedIngestionHealthError) as raised:
            engine.poll_once(provider, max_items=10)

        error = raised.exception
        self.assertIsInstance(error.__cause__, OSError)
        self.assertIsNone(error.delivery_error)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(bus.calls, 1)
        self.assertEqual(len(bus.events), 1)
        self.assertEqual(health.failure_calls, 0)
        self.assertEqual(health.success_calls, 1)
        self.assertEqual(error.outcome.source_id, "source")
        self.assertEqual(error.outcome.received, 1)
        self.assertEqual(error.outcome.accepted, 1)
        self.assertEqual(error.outcome.rejected, 0)
        self.assertEqual(error.outcome.cursor, "cursor-1")
        self.assertEqual(error.outcome.quality_flags, ())
        self.assertGreater(error.outcome.elapsed_seconds, 0)
        self.assertEqual(health.last_success["accepted"], 1)  # type: ignore[index]

        health.fail_success = False
        state = error.outcome.record_health(health)  # type: ignore[arg-type]
        recovered = error.outcome.stats(health_status=state.status)

        self.assertEqual(health.success_calls, 2)
        self.assertEqual(bus.calls, 1)
        self.assertEqual(recovered.accepted, 1)
        self.assertEqual(recovered.health_status, "healthy")

    def test_delivery_failure_is_preserved_if_health_write_also_fails(self):
        health = _TrackingHealthStore(fail_success=True)
        bus = _CommittedDeliveryFailingBus()
        provider = _SuccessfulProvider(ProviderBatch("source", (self._quote(),)))
        engine = IngestionEngine(
            bus,  # type: ignore[arg-type]
            health_store=health,  # type: ignore[arg-type]
            clock=lambda: "2026-09-14T08:00:01+00:00",
        )

        with self.assertRaises(CommittedIngestionHealthError) as raised:
            engine.poll_once(provider, max_items=10)

        error = raised.exception
        self.assertIsInstance(error.__cause__, OSError)
        self.assertIsInstance(error.delivery_error, MarketEventDeliveryError)
        self.assertEqual(error.delivery_error.accepted_count, 1)  # type: ignore[union-attr]
        self.assertEqual(error.outcome.accepted, 1)
        self.assertEqual(bus.calls, 1)
        self.assertEqual(health.success_calls, 1)
        self.assertEqual(health.failure_calls, 0)

    def test_retry_after_post_publish_unlock_failure_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            health = SourceHealthStore(Path(temporary_directory) / "source-health.json")
            bus = _CommittedBus()
            provider = _SuccessfulProvider(
                ProviderBatch("source", (self._quote(),), cursor="cursor-1")
            )
            engine = IngestionEngine(
                bus,  # type: ignore[arg-type]
                health_store=health,
                clock=lambda: "2026-09-14T08:00:01+00:00",
            )
            original_unlock = _SourceHealthWriterLock._unlock_handle

            def unlock_then_raise(handle):
                original_unlock(handle)
                raise OSError("unlock failed after durable publication")

            with mock.patch.object(
                _SourceHealthWriterLock,
                "_unlock_handle",
                side_effect=unlock_then_raise,
            ):
                with self.assertRaises(CommittedIngestionHealthError) as raised:
                    engine.poll_once(provider, max_items=10)

            error = raised.exception
            self.assertIsInstance(error.__cause__, OSError)
            durable = health.get("source")
            self.assertEqual(durable.poll_count, 1)
            self.assertEqual(durable.total_received, 1)
            self.assertEqual(durable.total_accepted, 1)
            self.assertEqual(durable.total_rejected, 0)

            with mock.patch.object(
                health,
                "record_success",
                side_effect=AssertionError("already-applied health must not be written twice"),
            ):
                recovered = error.outcome.record_health(health)

            self.assertEqual(recovered.poll_count, 1)
            self.assertEqual(recovered.total_received, 1)
            self.assertEqual(recovered.total_accepted, 1)
            self.assertEqual(recovered.total_rejected, 0)
            self.assertEqual(bus.calls, 1)

    def test_retry_fails_closed_when_health_state_diverged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            health = SourceHealthStore(Path(temporary_directory) / "source-health.json")
            bus = _CommittedBus()
            provider = _SuccessfulProvider(
                ProviderBatch("source", (self._quote(),), cursor="cursor-1")
            )
            engine = IngestionEngine(
                bus,  # type: ignore[arg-type]
                health_store=health,
                clock=lambda: "2026-09-14T08:00:01+00:00",
            )

            with mock.patch.object(
                health,
                "record_success",
                side_effect=OSError("health write failed before publication"),
            ):
                with self.assertRaises(CommittedIngestionHealthError) as raised:
                    engine.poll_once(provider, max_items=10)

            health.record_success(
                "source",
                now="2026-09-14T08:00:02+00:00",
                received=2,
                accepted=2,
                rejected=0,
                cursor="concurrent-cursor",
                latest_source_ts=None,
                quality_flags=(),
            )
            before_retry = health.get("source")

            with self.assertRaisesRegex(
                RuntimeError,
                "source health changed since the committed ingestion outcome",
            ):
                raised.exception.outcome.record_health(health)

            after_retry = health.get("source")
            self.assertEqual(after_retry.poll_count, before_retry.poll_count)
            self.assertEqual(after_retry.total_received, before_retry.total_received)
            self.assertEqual(after_retry.total_accepted, before_retry.total_accepted)
            self.assertEqual(after_retry.total_rejected, before_retry.total_rejected)
            self.assertEqual(bus.calls, 1)


if __name__ == "__main__":
    unittest.main()
