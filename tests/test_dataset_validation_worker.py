from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from autosport.dataset_worker import OneShotDatasetValidationWorker


class DatasetValidationWorkerTests(unittest.TestCase):
    def _terminal(
        self,
        worker: OneShotDatasetValidationWorker,
        timeout: float = 5.0,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = worker.poll()
            if message is not None:
                return message
            time.sleep(0.01)
        self.fail("dataset validation worker did not publish terminal message")

    def _assert_retry_succeeds(self, worker: OneShotDatasetValidationWorker) -> None:
        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_worker_is_single_flight_daemon_and_runs_away_from_caller(self) -> None:
        caller_thread = threading.get_ident()
        started = threading.Event()
        release = threading.Event()
        task_thread: list[int] = []
        sentinel = object()

        def task():
            task_thread.append(threading.get_ident())
            started.set()
            self.assertTrue(release.wait(2.0))
            return sentinel

        worker = OneShotDatasetValidationWorker()
        self.assertTrue(worker.start(task))
        self.assertTrue(started.wait(1.0))
        self.assertTrue(worker.busy)
        self.assertIsNotNone(worker._thread)
        self.assertTrue(worker._thread.daemon)
        self.assertNotEqual(task_thread, [caller_thread])
        self.assertFalse(worker.start(task))

        release.set()
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_baseexception_and_broken_stringification_are_terminal(self) -> None:
        worker = OneShotDatasetValidationWorker()

        class BrokenStringError(BaseException):
            def __str__(self) -> str:
                raise RuntimeError("broken formatter")

        def task():
            raise BrokenStringError()

        self.assertTrue(worker.start(task))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(
            failed.error,
            "BaseException: dataset validation failed; exception details unavailable",
        )
        self.assertFalse(worker.busy)
        self._assert_retry_succeeds(worker)

    def test_setup_failure_rolls_back_slot_and_allows_retry(self) -> None:
        worker = OneShotDatasetValidationWorker()
        with patch(
            "autosport.dataset_worker.threading.Thread",
            side_effect=OSError("constructor failed"),
        ):
            self.assertFalse(worker.start(lambda: self.fail("task must not run")))

        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())
        self._assert_retry_succeeds(worker)

    def test_partial_thread_start_failure_cancels_task_and_allows_retry(self) -> None:
        worker = OneShotDatasetValidationWorker()
        original_start = threading.Thread.start
        task_ran = threading.Event()

        def start_then_fail(thread) -> None:
            original_start(thread)
            raise OSError("late start failure")

        with patch(
            "autosport.dataset_worker.threading.Thread.start",
            new=start_then_fail,
        ):
            self.assertFalse(worker.start(lambda: task_ran.set()))

        self.assertFalse(task_ran.wait(0.1))
        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())
        self._assert_retry_succeeds(worker)

    def test_setup_process_control_exception_cleans_up_before_reraise(self) -> None:
        worker = OneShotDatasetValidationWorker()
        with patch(
            "autosport.dataset_worker.threading.Event",
            side_effect=KeyboardInterrupt("setup interrupted"),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "setup interrupted"):
                worker.start(lambda: self.fail("task must not run"))

        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())
        self._assert_retry_succeeds(worker)


if __name__ == "__main__":
    unittest.main()
