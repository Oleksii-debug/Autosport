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

    def test_worker_thread_start_failure_publishes_terminal_error_and_allows_retry(self):
        worker = OneShotReplayWorker()
        task_ran = threading.Event()
        sentinel = object()

        def task():
            task_ran.set()
            return sentinel

        with patch.object(
            threading.Thread,
            "start",
            side_effect=RuntimeError("can't start new thread"),
        ):
            self.assertTrue(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertFalse(worker.start(task))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "RuntimeError: can't start new thread")
        self.assertFalse(worker.busy)

        self.assertTrue(worker.start(task))
        message = self._terminal(worker)
        self.assertTrue(task_ran.is_set())
        self.assertIs(message.result, sentinel)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_partial_thread_start_exception_cancels_task_and_publishes_one_error(self):
        worker = OneShotReplayWorker()
        original_start = threading.Thread.start
        task_ran = threading.Event()

        def start_then_fail(thread) -> None:
            original_start(thread)
            raise OSError("late start failure")

        with patch.object(threading.Thread, "start", new=start_then_fail):
            self.assertTrue(worker.start(lambda: task_ran.set()))

        self.assertFalse(task_ran.wait(0.1))
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertFalse(worker.start(lambda: self.fail("retry must wait for poll")))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "OSError: late start failure")
        self.assertFalse(worker.busy)
        self.assertIsNone(worker.poll())

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_worker_partial_thread_start_baseexception_cancels_task_before_reraise(self):
        worker = OneShotReplayWorker()
        original_start = threading.Thread.start
        task_ran = threading.Event()

        def start_then_interrupt(thread) -> None:
            original_start(thread)
            raise KeyboardInterrupt("late start interrupt")

        with patch.object(threading.Thread, "start", new=start_then_interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "late start interrupt"):
                worker.start(lambda: task_ran.set())

        self.assertFalse(task_ran.wait(0.1))
        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_worker_thread_construction_exception_publishes_terminal_error(self):
        worker = OneShotReplayWorker()
        task_ran = threading.Event()

        def task():
            task_ran.set()
            return object()

        with patch(
            "autosport.replay_worker.threading.Thread",
            side_effect=OSError("thread allocation failed"),
        ):
            self.assertTrue(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertFalse(worker.start(task))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "OSError: thread allocation failed")
        self.assertFalse(worker.busy)

    def test_worker_thread_construction_unprintable_exception_is_terminal_and_retryable(self):
        worker = OneShotReplayWorker()
        task_ran = threading.Event()

        class BrokenStringError(Exception):
            def __str__(self) -> str:
                raise RuntimeError("broken exception formatter")

        def task():
            task_ran.set()
            return object()

        with patch(
            "autosport.replay_worker.threading.Thread",
            side_effect=BrokenStringError(),
        ):
            self.assertTrue(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(
            failed.error,
            "BrokenStringError: exception details unavailable",
        )
        self.assertFalse(worker.busy)

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_worker_thread_construction_broken_type_name_is_terminal_and_retryable(self):
        worker = OneShotReplayWorker()
        task_ran = threading.Event()

        class BrokenNameMeta(type):
            def __getattribute__(cls, name: str):
                if name == "__name__":
                    raise RuntimeError("broken exception type formatter")
                return super().__getattribute__(name)

        class BrokenNameError(Exception, metaclass=BrokenNameMeta):
            pass

        def task():
            task_ran.set()
            return object()

        with patch(
            "autosport.replay_worker.threading.Thread",
            side_effect=BrokenNameError("thread metadata failed"),
        ):
            self.assertTrue(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "BrokenNameError: thread metadata failed")
        self.assertFalse(worker.busy)

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_worker_reports_failure_without_raising_on_tk_poll_thread(self):
        worker = OneShotReplayWorker()
        self.assertTrue(worker.start(lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        message = self._terminal(worker)
        self.assertIsNone(message.result)
        self.assertEqual(message.error, "RuntimeError: boom")
        self.assertFalse(worker.busy)

    def test_worker_terminalizes_system_exit_and_allows_retry(self):
        worker = OneShotReplayWorker()
        self.assertTrue(worker.start(lambda: (_ for _ in ()).throw(SystemExit("stop"))))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "SystemExit: stop")
        self.assertFalse(worker.busy)

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        message = self._terminal(worker)
        self.assertIs(message.result, sentinel)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_terminalizes_unprintable_base_exception_and_allows_retry(self):
        worker = OneShotReplayWorker()

        class BrokenStringBaseError(BaseException):
            def __str__(self) -> str:
                raise RuntimeError("broken exception formatter")

        def task():
            raise BrokenStringBaseError()

        self.assertTrue(worker.start(task))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(
            failed.error,
            "BrokenStringBaseError: exception details unavailable",
        )
        self.assertFalse(worker.busy)

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_worker_terminalizes_broken_type_name_base_exception_and_allows_retry(self):
        worker = OneShotReplayWorker()

        class BrokenNameMeta(type):
            def __getattribute__(cls, name: str):
                if name == "__name__":
                    raise RuntimeError("broken exception type formatter")
                return super().__getattribute__(name)

        class BrokenNameBaseError(BaseException, metaclass=BrokenNameMeta):
            pass

        def task():
            raise BrokenNameBaseError("task metadata failed")

        self.assertTrue(worker.start(task))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "BrokenNameBaseError: task metadata failed")
        self.assertFalse(worker.busy)

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_worker_terminalizes_hostile_rendered_string_subclass_and_allows_retry(self):
        worker = OneShotReplayWorker()

        class HostileRenderedString(str):
            def __format__(self, spec: str) -> str:
                raise RuntimeError("hostile rendered-string formatter")

        class HostileRenderedError(BaseException):
            def __str__(self) -> str:
                return HostileRenderedString("rendered safely")

        def task():
            raise HostileRenderedError()

        self.assertTrue(worker.start(task))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "HostileRenderedError: rendered safely")
        self.assertFalse(worker.busy)

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
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
