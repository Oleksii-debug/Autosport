from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_strategy_workspace_resolution_preserves_baseline_and_isolates_plan_identity(self):
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

    def test_baseline_and_observe_only_can_run_without_mixed_strategy_workspace(self):
        dataset = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline_workspace = workspace_for_strategy(root, "baseline-v1")
            control_workspace = workspace_for_strategy(root, "observe-only-v1")
            baseline = run_workspace_dataset_once(
                baseline_workspace,
                dataset,
                strategy_id="baseline-v1",
            )
            control = run_workspace_dataset_once(
                control_workspace,
                dataset,
                strategy_id="observe-only-v1",
            )
            self.assertGreater(baseline.replay.event_count, 0)
            self.assertGreater(control.replay.event_count, 0)
            self.assertTrue((baseline_workspace / "paper_book.json").is_file())
            self.assertTrue((control_workspace / "paper_book.json").is_file())
            self.assertNotEqual(baseline_workspace, control_workspace)

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

    def test_research_strategy_fails_closed_without_typed_plan(self):
        dataset = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "requires --research-plan"):
                run_workspace_dataset_once(
                    temp,
                    dataset,
                    strategy_id="research-replay-v1",
                )


if __name__ == "__main__":
    unittest.main()
