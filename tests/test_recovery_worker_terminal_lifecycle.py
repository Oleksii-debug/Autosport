from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from autosport.recovery_worker import OneShotRecoveryWorker


class RecoveryWorkerTerminalLifecycleTests(unittest.TestCase):
    def _terminal(self, worker: OneShotRecoveryWorker, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = worker.poll()
            if message is not None:
                return message
            time.sleep(0.01)
        self.fail("recovery worker did not publish terminal message")

    def _assert_retry_succeeds(self, worker: OneShotRecoveryWorker) -> None:
        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        completed = self._terminal(worker)
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_system_exit_is_terminal_and_worker_can_retry(self) -> None:
        worker = OneShotRecoveryWorker()

        def exits() -> None:
            raise SystemExit("recovery-stop")

        self.assertTrue(worker.start(exits))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "SystemExit: recovery-stop")
        self.assertFalse(worker.busy)

        self._assert_retry_succeeds(worker)

    def test_unprintable_base_exception_is_terminal_and_worker_can_retry(self) -> None:
        worker = OneShotRecoveryWorker()

        class BrokenStringError(BaseException):
            def __str__(self) -> str:
                raise RuntimeError("broken exception formatter")

        def fails_with_unprintable_error() -> None:
            raise BrokenStringError()

        self.assertTrue(worker.start(fails_with_unprintable_error))
        failed = self._terminal(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(
            failed.error,
            "BaseException: recovery task failed; exception details unavailable",
        )
        self.assertFalse(worker.busy)

        self._assert_retry_succeeds(worker)

    def test_thread_constructor_exception_rolls_back_busy_and_allows_retry(self) -> None:
        worker = OneShotRecoveryWorker()
        with patch(
            "autosport.recovery_worker.threading.Thread",
            side_effect=OSError("constructor failed"),
        ):
            self.assertFalse(worker.start(lambda: self.fail("task must not run")))

        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())
        self._assert_retry_succeeds(worker)

    def test_thread_start_non_runtime_exception_rolls_back_busy_and_allows_retry(self) -> None:
        worker = OneShotRecoveryWorker()
        with patch(
            "autosport.recovery_worker.threading.Thread.start",
            side_effect=OSError("start failed"),
        ):
            self.assertFalse(worker.start(lambda: self.fail("task must not run")))

        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())
        self._assert_retry_succeeds(worker)

    def test_thread_constructor_baseexception_cleans_up_before_reraise(self) -> None:
        worker = OneShotRecoveryWorker()
        with patch(
            "autosport.recovery_worker.threading.Thread",
            side_effect=KeyboardInterrupt("constructor interrupted"),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "constructor interrupted"):
                worker.start(lambda: self.fail("task must not run"))

        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())
        self._assert_retry_succeeds(worker)

    def test_thread_start_baseexception_cleans_up_before_reraise(self) -> None:
        worker = OneShotRecoveryWorker()
        with patch(
            "autosport.recovery_worker.threading.Thread.start",
            side_effect=SystemExit("start interrupted"),
        ):
            with self.assertRaisesRegex(SystemExit, "start interrupted"):
                worker.start(lambda: self.fail("task must not run"))

        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())
        self._assert_retry_succeeds(worker)


if __name__ == "__main__":
    unittest.main()
