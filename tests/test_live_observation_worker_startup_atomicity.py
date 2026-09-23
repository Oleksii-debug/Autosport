from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from autosport.live_observation import OneShotObservationWorker


def _live_observation_helpers() -> tuple[threading.Thread, ...]:
    return tuple(
        thread
        for thread in threading.enumerate()
        if thread.name == "autosport-live-observation" and thread.is_alive()
    )


class LiveObservationWorkerStartupAtomicityTests(unittest.TestCase):
    def test_partial_thread_start_exception_reaps_helper_before_terminal_error(self):
        worker = OneShotObservationWorker()
        original_start = threading.Thread.start
        task_ran = threading.Event()

        def start_then_fail(thread: threading.Thread) -> None:
            original_start(thread)
            raise OSError("late start failure")

        with patch.object(threading.Thread, "start", new=start_then_fail):
            self.assertTrue(worker.start(lambda: task_ran.set()))

        self.assertEqual(_live_observation_helpers(), ())
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

    def test_partial_thread_start_baseexception_reaps_helper_before_slot_release(self):
        worker = OneShotObservationWorker()
        original_start = threading.Thread.start
        task_ran = threading.Event()

        def start_then_interrupt(thread: threading.Thread) -> None:
            original_start(thread)
            raise KeyboardInterrupt("late start interrupt")

        with patch.object(threading.Thread, "start", new=start_then_interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "late start interrupt"):
                worker.start(lambda: task_ran.set())

        self.assertEqual(_live_observation_helpers(), ())
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

    def test_prelaunch_start_exception_never_joins_unstarted_thread(self):
        worker = OneShotObservationWorker()
        task_ran = threading.Event()

        with patch.object(threading.Thread, "join", side_effect=AssertionError("unstarted join")):
            with patch.object(threading.Thread, "start", side_effect=OSError("pre-start failure")):
                self.assertTrue(worker.start(lambda: task_ran.set()))

        self.assertFalse(task_ran.is_set())
        self.assertEqual(_live_observation_helpers(), ())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        failed = worker.poll()
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertEqual(failed.error, "OSError: pre-start failure")
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
