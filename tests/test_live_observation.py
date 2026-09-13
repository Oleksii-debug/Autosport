import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.live_observation import OneShotObservationWorker, observe_workspace_once
from autosport.providers import InMemoryProvider, ProviderQuote
from autosport.ui_model import observation_quote_lines, observation_summary


_RECEIVE_TIME = "2026-09-12T20:00:02+00:00"


class LiveObservationTests(unittest.TestCase):
    @staticmethod
    def _provider() -> InMemoryProvider:
        return InMemoryProvider(
            "live-fixture",
            [
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-a",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-12T20:00:00+00:00",
                    sequence=1,
                    source_ts="2026-09-12T19:59:59+00:00",
                ),
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-b",
                    decimal_odds=Decimal("2.05"),
                    observed_ts="2026-09-12T20:00:00+00:00",
                    sequence=2,
                    source_ts="2026-09-12T19:59:59+00:00",
                ),
            ],
        )

    @staticmethod
    def _observe(workspace: str | Path):
        return observe_workspace_once(
            workspace,
            LiveObservationTests._provider(),
            max_items=10,
            clock=lambda: _RECEIVE_TIME,
        )

    @staticmethod
    def _wait_for_message(worker: OneShotObservationWorker, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = worker.poll()
            if message is not None:
                return message
            time.sleep(0.01)
        raise AssertionError("worker did not publish terminal message")

    def test_workspace_observer_uses_short_lived_market_and_health_stores_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._observe(tmp)
            self.assertEqual(result.stats.accepted, 2)
            self.assertEqual(result.health.status, "healthy")
            self.assertEqual(len(result.current_quotes), 2)
            self.assertTrue((Path(tmp) / "market.db").exists())
            self.assertTrue((Path(tmp) / "source_health.json").exists())
            self.assertFalse((Path(tmp) / "paper_book.json").exists())

    def test_worker_refuses_second_start_until_terminal_message_is_consumed(self):
        # Build the real observation result outside the worker timing window. This
        # test owns the worker single-flight/message-consumption contract; SQLite
        # startup latency is covered by the workspace-observer integration test and
        # must not become an accidental two-second completion SLA for live I/O.
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._observe(tmp)

        worker = OneShotObservationWorker()
        entered = threading.Event()
        release = threading.Event()

        def slow_task():
            entered.set()
            release.wait(timeout=2)
            return expected

        self.assertTrue(worker.start(slow_task))
        self.assertTrue(entered.wait(timeout=1))
        self.assertTrue(worker.busy)
        self.assertFalse(worker.start(slow_task))
        release.set()
        message = self._wait_for_message(worker)
        self.assertIs(message.result, expected)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_thread_is_non_daemon_while_durable_observation_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._observe(tmp)

        worker = OneShotObservationWorker()
        entered = threading.Event()
        release = threading.Event()

        def slow_task():
            entered.set()
            release.wait(timeout=2)
            return expected

        self.assertTrue(worker.start(slow_task))
        self.assertTrue(entered.wait(timeout=1))
        self.assertTrue(worker.busy)
        self.assertIsNotNone(worker._thread)
        self.assertFalse(worker._thread.daemon)
        release.set()
        message = self._wait_for_message(worker)
        self.assertIs(message.result, expected)
        self.assertFalse(worker.busy)

    def test_worker_converts_exception_to_terminal_error_message(self):
        worker = OneShotObservationWorker()
        self.assertTrue(worker.start(lambda: (_ for _ in ()).throw(RuntimeError("network-test"))))
        message = self._wait_for_message(worker)
        self.assertIsNone(message.result)
        self.assertEqual(message.error, "RuntimeError: network-test")
        self.assertFalse(worker.busy)

    def test_presentation_is_deterministic_text_for_screen_reader_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._observe(tmp)
            summary = observation_summary(result)
            lines = observation_quote_lines(result)
            self.assertIn("health=healthy", summary)
            self.assertIn("current=2", summary)
            self.assertEqual(len(lines), 2)
            self.assertIn("player-a", lines[0])
            self.assertIn("odds 1.80", lines[0])
            self.assertIn("source time 2026-09-12T19:59:59+00:00", lines[0])


if __name__ == "__main__":
    unittest.main()
