from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from autosport.live_observation import OneShotObservationWorker


class LiveObservationWorkerStartupAtomicityTests(unittest.TestCase):
    def test_partial_thread_start_exception_cancels_durable_task_and_publishes_one_error(self):
        worker = OneShotObservationWorker()
        original_start = threading.Thread.start
        task_ran = threading.Event()
        launched: list[threading.Thread] = []

        def start_then_fail(thread: threading.Thread) -> None:
            launched.append(thread)
            original_start(thread)
            raise OSError("late start failure")

        with patch.object(threading.Thread, "start", new=start_then_fail):
            self.assertTrue(worker.start(lambda: task_ran.set()))

        self.assertEqual(len(launched), 1)
        launched[0].join(timeout=1.0)
        self.assertFalse(launched[0].is_alive())
        self.assertFalse(task_ran.is_set())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertFalse(worker.start(lambda: self.fail("retry must wait for poll")))

        failed = worker.poll()
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "OSError: late start failure")
        self.assertFalse(worker.busy)
        self.assertIsNone(worker.poll())

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        launched_retry = worker._thread
        self.assertIsNotNone(launched_retry)
        assert launched_retry is not None
        launched_retry.join(timeout=1.0)
        completed = worker.poll()
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_partial_thread_start_baseexception_cancels_task_and_rolls_back_slot(self):
        worker = OneShotObservationWorker()
        original_start = threading.Thread.start
        task_ran = threading.Event()
        launched: list[threading.Thread] = []

        def start_then_interrupt(thread: threading.Thread) -> None:
            launched.append(thread)
            original_start(thread)
            raise KeyboardInterrupt("late start interrupt")

        with patch.object(threading.Thread, "start", new=start_then_interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "late start interrupt"):
                worker.start(lambda: task_ran.set())

        self.assertEqual(len(launched), 1)
        launched[0].join(timeout=1.0)
        self.assertFalse(launched[0].is_alive())
        self.assertFalse(task_ran.is_set())
        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        retry = worker._thread
        self.assertIsNotNone(retry)
        assert retry is not None
        retry.join(timeout=1.0)
        completed = worker.poll()
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_thread_constructor_baseexception_rolls_back_slot_without_terminal_message(self):
        worker = OneShotObservationWorker()
        task_ran = threading.Event()

        with patch(
            "autosport.live_observation.threading.Thread",
            side_effect=KeyboardInterrupt("thread construction interrupted"),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "thread construction interrupted"):
                worker.start(lambda: task_ran.set())

        self.assertFalse(task_ran.is_set())
        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())

        sentinel = object()
        self.assertTrue(worker.start(lambda: sentinel))
        retry = worker._thread
        self.assertIsNotNone(retry)
        assert retry is not None
        retry.join(timeout=1.0)
        completed = worker.poll()
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertIs(completed.result, sentinel)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)


if __name__ == "__main__":
    unittest.main()
