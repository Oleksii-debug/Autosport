import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import (
    CollectorServiceConfig,
    HeadlessCollectorService,
    _SignalStopRequest,
)
from autosport.event_lifecycle import ContinuousEventLifecycle
from autosport.providers import ProviderUnavailableError


class _UnavailableSource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(self) -> None:
        self.catalog_calls = 0

    def fetch_catalog_page(self, _checkpoint):
        self.catalog_calls += 1
        raise ProviderUnavailableError("provider temporarily unavailable")

    def fetch_deltas(self, _checkpoint, _records, _max_items):
        raise AssertionError("delta acquisition must not follow failed catalog acquisition")


class _TripDuringWaitEvent:
    def __init__(self) -> None:
        self._set = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        self._set = True
        return True


class CollectorInterruptibleProviderBackoffTests(unittest.TestCase):
    def test_signal_stop_interrupts_provider_retry_backoff_before_second_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _UnavailableSource()
            stop = _SignalStopRequest()
            fake_event = _TripDuringWaitEvent()
            stop._event = fake_event

            sleep_calls: list[float] = []
            service = HeadlessCollectorService(
                delta_store=CollectorDeltaStore(root / "collector.db"),
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=source,
                state_path=root / "service.json",
                run_id="run-interruptible-backoff",
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=3,
                    initial_backoff_seconds=7,
                    max_backoff_seconds=7,
                    jitter_fraction=0,
                ),
                clock=lambda: "2026-09-22T02:00:00+00:00",
                sleep=sleep_calls.append,
                random_value=lambda: 0,
                stop_requested=stop,
                stop_reason=stop.reason,
            )

            result = service.run(max_cycles=3)

            self.assertEqual(result.cycles_executed, 0)
            self.assertIsNone(result.last_cycle)
            self.assertEqual(source.catalog_calls, 1)
            self.assertEqual(fake_event.waits, [7])
            self.assertEqual(sleep_calls, [])
            self.assertEqual(service.status()["stop_reason"], "stop_requested")
            self.assertIsNotNone(service.status()["stopped_at"])


if __name__ == "__main__":
    unittest.main()
