from __future__ import annotations

import inspect
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from autosport.gui import AUTOMATION_IDS
from autosport.recovery import RecoveryReport
from autosport.recovery_worker import (
    OneShotRecoveryWorker,
    RecoveryTaskResult,
    RecoveryWorkerMessage,
    recover_workspace_once,
)
from autosport.replay_worker import workspace_for_strategy
from autosport.windows_gui import WindowsAutosportApp
import autosport.windows_entry as windows_entry


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _BusyWorker:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _CapturedRecoveryWorker:
    def __init__(self) -> None:
        self.busy = False
        self.task = None
        self.message: RecoveryWorkerMessage | None = None

    def start(self, task) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.task = task
        return True

    def poll(self) -> RecoveryWorkerMessage | None:
        if self.message is None:
            return None
        message = self.message
        self.message = None
        self.busy = False
        return message


class GuiWorkspaceRecoveryTests(unittest.TestCase):
    def _app(self, root: Path):
        app = object.__new__(WindowsAutosportApp)
        app._closing = False
        app.workspace = root
        app._active_workspace = root
        app._active_strategy_id = "baseline-v1"
        app._active_research_plan = None
        app._recovery_view = None
        app._recovery_blocked_workspace = None
        app.replay_worker = _BusyWorker(False)
        app.live_worker = _BusyWorker(False)
        app.recovery_worker = _CapturedRecoveryWorker()
        app.session = _Session()
        app.status = _Value()
        app.bank = _Value()
        app.live_status = _Value()
        app._busy_states = []
        app._logs = []
        app._scheduled = []
        app._ticket_refreshes = 0
        app._set_replay_controls_busy = lambda busy: app._busy_states.append(busy)
        app._append_log = lambda text: app._logs.append(text)
        app.after = lambda delay, callback: app._scheduled.append((delay, callback.__name__))
        app._refresh_tickets = lambda: setattr(app, "_ticket_refreshes", app._ticket_refreshes + 1)
        app.bell = lambda: setattr(app, "_bell_rang", True)
        return app

    def test_packaged_windows_entry_uses_responsive_gui(self) -> None:
        source = inspect.getsource(windows_entry._run_interactive_gui)
        self.assertIn("from autosport.windows_gui import main as gui_main", source)
        self.assertEqual(AUTOMATION_IDS["repair_workspace"], 108)

    def test_recovery_worker_is_non_daemon_and_runs_off_caller_thread(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = OneShotRecoveryWorker()
            caller_thread = threading.get_ident()
            task_threads: list[int] = []

            def task():
                task_threads.append(threading.get_ident())
                return recover_workspace_once(root)

            self.assertTrue(worker.start(task))
            thread = worker._thread
            self.assertIsNotNone(thread)
            assert thread is not None
            self.assertFalse(thread.daemon)
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            message = worker.poll()
            self.assertIsNotNone(message)
            assert message is not None
            self.assertIsNone(message.error)
            self.assertIsNotNone(message.result)
            self.assertEqual(len(task_threads), 1)
            self.assertNotEqual(task_threads[0], caller_thread)
            self.assertFalse(worker.busy)

    def test_recovery_worker_thread_start_failure_rolls_back_busy_and_allows_retry(self) -> None:
        worker = OneShotRecoveryWorker()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            self.assertFalse(worker.start(lambda: self.fail("task must not run when thread start fails")))

        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(worker.start(lambda: recover_workspace_once(root)))
            thread = worker._thread
            self.assertIsNotNone(thread)
            assert thread is not None
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            message = worker.poll()
            self.assertIsNotNone(message)
            assert message is not None
            self.assertIsNone(message.error)
            self.assertIsNotNone(message.result)
            self.assertFalse(worker.busy)

    def test_gui_recovery_uses_selected_research_plan_workspace_and_terminal_view(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = SimpleNamespace(
                source_sha256="a" * 64,
                experiment_strategy_id="research-replay-v1@" + "a" * 64,
            )
            app = self._app(root)
            original_session = app.session
            app._selected_replay_configuration = lambda: ("research-replay-v1", plan)
            expected_workspace = workspace_for_strategy(root, "research-replay-v1", plan)

            WindowsAutosportApp.repair_workspace(app)

            self.assertTrue(original_session.closed)
            self.assertIsNone(app.session)
            self.assertEqual(app._active_workspace, expected_workspace)
            self.assertEqual(app._recovery_blocked_workspace, expected_workspace)
            self.assertEqual(app._busy_states, [True])
            self.assertEqual(app._scheduled, [(100, "_poll_recovery_worker")])
            worker = app.recovery_worker
            self.assertIsNotNone(worker.task)
            result = worker.task()
            self.assertEqual(result.session_view.workspace, expected_workspace)
            self.assertEqual(result.session_view.strategy_id, plan.experiment_strategy_id)
            self.assertFalse(hasattr(result.session_view, "store"))
            worker.message = RecoveryWorkerMessage(result=result)

            with patch("autosport.windows_gui.messagebox.showinfo") as info:
                WindowsAutosportApp._poll_recovery_worker(app)

            self.assertEqual(app._busy_states, [True, False])
            self.assertIs(app._recovery_view, result.session_view)
            self.assertIsNone(app._recovery_blocked_workspace)
            self.assertIn("Workspace готовий", app.status.value)
            self.assertGreaterEqual(app._ticket_refreshes, 2)
            info.assert_called_once()

    def test_unresolved_recovery_keeps_economic_workspace_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self._app(root)
            terminal = recover_workspace_once(root)
            unresolved = RecoveryTaskResult(
                report=RecoveryReport((), (), ("experiment-key",)),
                session_view=terminal.session_view,
            )
            # A terminal worker result is only polled after repair_workspace() has
            # closed and detached the prior Tk-owned session. Mirror that real
            # lifecycle here so the regression exercises the worker view rather
            # than an impossible stale UI test double.
            app.session = None
            app._recovery_blocked_workspace = root
            app.recovery_worker.message = RecoveryWorkerMessage(result=unresolved)

            with patch("autosport.windows_gui.messagebox.showwarning") as warning:
                WindowsAutosportApp._poll_recovery_worker(app)

            self.assertEqual(app._recovery_blocked_workspace, root)
            self.assertIn("unresolved", app.status.value)
            warning.assert_called_once()

    def test_recovery_error_never_reopens_sqlite_session_on_tk_thread(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self._app(root)
            app.session = None
            app._recovery_blocked_workspace = root
            app._open_session = lambda *_args, **_kwargs: self.fail("Tk thread must not reopen recovery session")
            app.recovery_worker.message = RecoveryWorkerMessage(error="ReconciliationError: corrupted state")

            with patch("autosport.windows_gui.messagebox.showerror") as error:
                WindowsAutosportApp._poll_recovery_worker(app)

            self.assertIsNone(app.session)
            self.assertIsNone(app._recovery_view)
            self.assertEqual(app._recovery_blocked_workspace, root)
            self.assertIn("заблоковано", app.status.value)
            error.assert_called_once()

    def test_recovery_busy_blocks_replay_live_second_recovery_and_close(self) -> None:
        with TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            app.recovery_worker.busy = True
            app._bell_rang = False

            WindowsAutosportApp.run_dataset(app)
            self.assertIn("recovery ще виконується", app.status.value)

            WindowsAutosportApp.refresh_live_snapshot(app)
            self.assertIn("не запускається одночасно", app.status.value)

            WindowsAutosportApp.repair_workspace(app)
            self.assertIn("уже виконується", app.status.value)

            WindowsAutosportApp.close_app(app)
            self.assertTrue(app._bell_rang)
            self.assertIn("Закриття програми заблоковано", app.status.value)

    def test_failed_workspace_is_blocked_but_other_strategy_workspace_is_not_globally_poisoned(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self._app(root)
            app._recovery_blocked_workspace = root
            app._selected_replay_configuration = lambda: ("baseline-v1", None)

            WindowsAutosportApp.run_dataset(app)
            self.assertIn("заблоковано fail-closed", app.status.value)


if __name__ == "__main__":
    unittest.main()
