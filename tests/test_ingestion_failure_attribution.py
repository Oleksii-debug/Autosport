import unittest
from decimal import Decimal
from types import SimpleNamespace

from autosport.ingestion import IngestionEngine
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
    def __init__(self, *, fail_get: bool = False) -> None:
        self.fail_get = fail_get
        self.failure_calls = 0

    def get(self, source_id: str):
        if self.fail_get:
            raise RuntimeError("local health read failed")
        return SimpleNamespace(latest_source_ts=None)

    def record_failure(self, source_id: str, *, now: str, error: BaseException):
        self.failure_calls += 1
        return None


class _UnusedBus:
    def publish_many(self, events):
        raise AssertionError("local failure must occur before persistence")


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


if __name__ == "__main__":
    unittest.main()
