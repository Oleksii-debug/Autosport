from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.recovery import RecoveryReport
from autosport.recovery_worker import OneShotRecoveryWorker, recover_workspace_once


class _FakeBook:
    balance = "10000"
    committed_stake = "0"
    tickets: dict = {}


class RecoveryWorkerTests(unittest.TestCase):
    def test_reconcile_reopen_and_close_run_off_caller_thread(self) -> None:
        caller_thread = threading.get_ident()
        started = threading.Event()
        release = threading.Event()
        reconcile_threads: list[int] = []
        reopen_threads: list[int] = []
        close_threads: list[int] = []

        def fake_reconcile(_workspace: Path) -> RecoveryReport:
            reconcile_threads.append(threading.get_ident())
            started.set()
            self.assertTrue(release.wait(2), "test did not release recovery worker")
            return RecoveryReport(("reconciled",), (), ())

        class FakeSession:
            def __init__(self, workspace, _initial_bankroll, *, strategy_id, research_plan):
                reopen_threads.append(threading.get_ident())
                self.workspace = Path(workspace)
                self.strategy_id = strategy_id
                self.research_plan = research_plan
                self.book = _FakeBook()

            def close(self) -> None:
                close_threads.append(threading.get_ident())

        worker = OneShotRecoveryWorker()
        with (
            patch("autosport.recovery_worker.reconcile_late_crashes", side_effect=fake_reconcile),
            patch("autosport.recovery_worker.AutosportSession", FakeSession),
        ):
            self.assertTrue(
                worker.start(lambda: recover_workspace_once("workspace", "baseline-v1"))
            )
            self.assertTrue(started.wait(2), "recovery worker did not start")
            self.assertTrue(worker.busy)
            self.assertIsNone(worker.poll(), "poll must not block waiting for recovery")
            self.assertNotEqual(reconcile_threads, [caller_thread])
            release.set()

            message = None
            for _ in range(200):
                message = worker.poll()
                if message is not None:
                    break
                time.sleep(0.01)

        self.assertIsNotNone(message, "worker did not publish terminal recovery result")
        assert message is not None
        self.assertIsNone(message.error)
        self.assertIsNotNone(message.result)
        assert message.result is not None
        self.assertEqual(message.result.report, RecoveryReport(("reconciled",), (), ()))
        self.assertIsNone(message.result.recovery_error)
        self.assertIsNone(message.result.reopen_error)
        self.assertIsNotNone(message.result.snapshot)
        assert message.result.snapshot is not None
        self.assertEqual(message.result.snapshot.balance, "10000")
        self.assertEqual(message.result.snapshot.committed_stake, "0")
        self.assertEqual(message.result.snapshot.ticket_lines, ("Paper tickets ще відсутні.",))
        self.assertFalse(worker.busy)
        self.assertEqual(len(reconcile_threads), 1)
        self.assertEqual(reopen_threads, reconcile_threads)
        self.assertEqual(close_threads, reconcile_threads)
        self.assertNotEqual(reconcile_threads[0], caller_thread)

    def test_recovery_failure_still_attempts_read_only_session_snapshot(self) -> None:
        class FakeSession:
            def __init__(self, workspace, _initial_bankroll, *, strategy_id, research_plan):
                self.workspace = Path(workspace)
                self.strategy_id = strategy_id
                self.research_plan = research_plan
                self.book = _FakeBook()

            def close(self) -> None:
                return None

        with (
            patch(
                "autosport.recovery_worker.reconcile_late_crashes",
                side_effect=RuntimeError("recovery failed"),
            ),
            patch("autosport.recovery_worker.AutosportSession", FakeSession),
        ):
            result = recover_workspace_once("workspace", "baseline-v1")

        self.assertIsNone(result.report)
        self.assertIn("RuntimeError: recovery failed", result.recovery_error or "")
        self.assertIsNone(result.reopen_error)
        self.assertIsNotNone(result.snapshot)


if __name__ == "__main__":
    unittest.main()
