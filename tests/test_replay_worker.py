from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.replay_worker import (
    OneShotReplayWorker,
    run_workspace_dataset_once,
    workspace_for_strategy,
)


class ReplayWorkerTests(unittest.TestCase):
    def _terminal(self, worker: OneShotReplayWorker, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = worker.poll()
            if message is not None:
                return message
            time.sleep(0.01)
        self.fail("worker did not publish terminal message")

    def test_worker_is_single_flight_and_non_daemon_for_economic_replay(self):
        started = threading.Event()
        release = threading.Event()
        sentinel = object()

        def task():
            started.set()
            self.assertTrue(release.wait(2.0))
            return sentinel

        worker = OneShotReplayWorker()
        self.assertTrue(worker.start(task))
        self.assertTrue(started.wait(1.0))
        self.assertTrue(worker.busy)
        self.assertIsNotNone(worker._thread)
        self.assertFalse(worker._thread.daemon)
        self.assertFalse(worker.start(task))
        release.set()
        message = self._terminal(worker)
        self.assertIs(message.result, sentinel)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_reports_failure_without_raising_on_tk_poll_thread(self):
        worker = OneShotReplayWorker()
        self.assertTrue(worker.start(lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        message = self._terminal(worker)
        self.assertIsNone(message.result)
        self.assertEqual(message.error, "RuntimeError: boom")
        self.assertFalse(worker.busy)

    def test_strategy_workspace_keeps_legacy_baseline_and_isolates_other_identities(self):
        root = Path("root")
        self.assertEqual(workspace_for_strategy(root, "baseline-v1"), root)
        self.assertEqual(
            workspace_for_strategy(root, "observe-only-v1"),
            root / "strategies" / "observe-only-v1",
        )
        digest = "a" * 64
        plan = SimpleNamespace(
            experiment_strategy_id=f"research-replay-v1@{digest}",
            source_sha256=digest,
        )
        self.assertEqual(
            workspace_for_strategy(root, "research-replay-v1", plan),
            root / "strategies" / f"research-replay-v1-{digest}",
        )

    def test_workspace_runner_forwards_exact_strategy_and_plan_to_session(self):
        dataset = object()
        plan = object()
        result = object()
        with patch("autosport.replay_worker.load_dataset", return_value=dataset), patch(
            "autosport.replay_worker.AutosportSession"
        ) as session_type:
            session = session_type.return_value
            session.run_dataset.return_value = result
            actual = run_workspace_dataset_once(
                "strategy-workspace",
                "dataset-path",
                initial_bankroll="1234",
                speed=100.0,
                strategy_id="research-replay-v1",
                research_plan=plan,
            )
        self.assertIs(actual, result)
        session_type.assert_called_once_with(
            "strategy-workspace",
            "1234",
            strategy_id="research-replay-v1",
            research_plan=plan,
        )
        session.run_dataset.assert_called_once_with(dataset, speed=100.0)
        session.close.assert_called_once_with()

    def test_workspace_replay_owns_session_on_worker_thread_and_persists_result(self):
        dataset = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"
        with tempfile.TemporaryDirectory() as temp:
            worker = OneShotReplayWorker()
            self.assertTrue(
                worker.start(
                    lambda: run_workspace_dataset_once(
                        temp,
                        dataset,
                        initial_bankroll="10000",
                        speed=0.0,
                    )
                )
            )
            message = self._terminal(worker, timeout=10.0)
            self.assertIsNone(message.error)
            self.assertIsNotNone(message.result)
            self.assertGreater(message.result.replay.event_count, 0)
            self.assertTrue(Path(temp, "paper_book.json").is_file())
            self.assertTrue(Path(message.result.result_path).is_file())


if __name__ == "__main__":
    unittest.main()
