from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from autosport.gui import AUTOMATION_IDS
from autosport.localization import text
from autosport.replay import (
    FutureLeakageError,
    ReplayEngine,
    ReplayLeakageFirewall,
    ReplayStopRequested,
    ReplayStopToken,
)
from autosport.replay_worker import OneShotReplayWorker, run_workspace_dataset_once
from autosport.windows_replay_stop import REPLAY_STOP_AUTOMATION_ID


@dataclass(frozen=True)
class _ReplayEvent:
    observed_ts: str
    sequence: int
    dedupe_key: str

    def to_dict(self) -> dict[str, object]:
        return {
            "observed_ts": self.observed_ts,
            "sequence": self.sequence,
            "dedupe_key": self.dedupe_key,
        }


class ReplayStopTests(unittest.TestCase):
    def test_stop_token_serializes_request_against_completion(self):
        token = ReplayStopToken()
        completed: list[bool] = []

        self.assertTrue(token.request())
        with self.assertRaisesRegex(ReplayStopRequested, "stopped by operator"):
            token.finish(lambda: completed.append(True))
        self.assertEqual(completed, [])
        self.assertFalse(token.accepting)
        self.assertFalse(token.request())

        fresh = ReplayStopToken()
        fresh.finish(lambda: completed.append(True))
        self.assertEqual(completed, [True])
        self.assertFalse(fresh.request())

    def test_worker_stop_interrupts_realtime_wait_and_keeps_firewall_sealed(self):
        first_event_seen = threading.Event()
        firewall = ReplayLeakageFirewall({"event-2": "winner"})
        events = [
            _ReplayEvent("2026-09-21T00:00:00+00:00", 1, "event-1"),
            _ReplayEvent("2026-09-21T00:10:00+00:00", 2, "event-2"),
        ]
        worker = OneShotReplayWorker()

        def task():
            engine = ReplayEngine(events, firewall)

            def consume(event) -> None:
                if event.sequence == 1:
                    first_event_seen.set()

            return engine.run(consume, speed=1.0)

        self.assertTrue(worker.start(task))
        self.assertTrue(first_event_seen.wait(1.0))
        self.assertTrue(worker.stop_available)
        self.assertTrue(worker.request_stop())
        thread = worker._thread
        self.assertIsNotNone(thread)
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(worker.stopped_pending)

        message = worker.poll()
        self.assertIsNotNone(message)
        self.assertTrue(message.stopped)
        self.assertIsNone(message.result)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)
        self.assertFalse(worker.stop_available)
        self.assertFalse(worker.stopped_pending)
        with self.assertRaises(FutureLeakageError):
            firewall.result_for("event-2")

    def test_stop_terminal_keeps_workspace_retryable_without_economic_commit(self):
        dataset = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            worker = OneShotReplayWorker()
            with patch(
                "autosport.session.ReplayEngine.run",
                side_effect=ReplayStopRequested("paper replay stopped by operator"),
            ):
                self.assertTrue(
                    worker.start(
                        lambda: run_workspace_dataset_once(
                            workspace,
                            dataset,
                            initial_bankroll="10000",
                            speed=0.0,
                        )
                    )
                )
                thread = worker._thread
                self.assertIsNotNone(thread)
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())

            message = worker.poll()
            self.assertIsNotNone(message)
            self.assertTrue(message.stopped)
            self.assertIsNone(message.error)
            self.assertIsNone(message.result)

            # The session's existing pre-PRECOMMIT recovery path must leave the
            # exact workspace reusable; a normal replay is the strongest bounded
            # proof that STOP did not strand durable economic authority.
            result = run_workspace_dataset_once(
                workspace,
                dataset,
                initial_bankroll="10000",
                speed=0.0,
            )
            self.assertGreater(result.replay.event_count, 0)
            self.assertTrue(result.result_path.is_file())

    def test_windows_stop_surface_uses_catalog_and_unique_uia_id(self):
        self.assertEqual(text("ui.windows.replay_stop.button"), "Зупинити повтор")
        self.assertIn("Control+S", text("ui.windows.replay_stop.accessibility.description"))
        self.assertNotIn(REPLAY_STOP_AUTOMATION_ID, set(AUTOMATION_IDS.values()))
        self.assertEqual(REPLAY_STOP_AUTOMATION_ID, 110)


if __name__ == "__main__":
    unittest.main()
