from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from autosport.recovery_worker import OneShotRecoveryWorker


class RecoveryWorkerLifecycleTests(unittest.TestCase):
    def _terminal(self, worker: OneShotRecoveryWorker):
        thread = worker._thread
        self.assertIsNotNone(thread)
        assert thread is not None
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        message = worker.poll()
        self.assertIsNotNone(message)
        return message

    def test_thread_construction_exception_rolls_back_busy_and_allows_retry(self) -> None:
        worker = OneShotRecoveryWorker()
        task_ran = threading.Event()

        def task():
            task_ran.set()
            return object()

        with patch(
            "autosport.recovery_worker.threading.Thread",
            side_effect=OSError("thread allocation failed"),
        ):
            self.assertFalse(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        message = self._terminal(worker)
        self.assertIs(message.result, sentinel)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_thread_start_exception_rolls_back_busy_and_allows_retry(self) -> None:
        worker = OneShotRecoveryWorker()
        task_ran = threading.Event()

        def task():
            task_ran.set()
            return object()

        with patch.object(
            threading.Thread,
            "start",
            side_effect=OSError("thread start failed"),
        ):
            self.assertFalse(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        message = self._terminal(worker)
        self.assertIs(message.result, sentinel)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_terminalizes_system_exit_and_allows_retry(self) -> None:
        worker = OneShotRecoveryWorker()
        self.assertTrue(
            worker.start(lambda: (_ for _ in ()).throw(SystemExit("stop")))
        )

        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "SystemExit: stop")
        self.assertFalse(worker.busy)

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        recovered = self._terminal(worker)
        self.assertIs(recovered.result, sentinel)
        self.assertIsNone(recovered.error)
        self.assertFalse(worker.busy)


if __name__ == "__main__":
    unittest.main()
