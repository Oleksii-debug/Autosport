from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autosport.gui import AutosportApp
from autosport.recovery_worker import RecoveryWorkerMessage
from autosport.replay_worker import ReplayWorkerMessage
from autosport.windows_gui import WindowsAutosportApp


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _BusyWorker:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy


class _TerminalReplayWorker:
    def __init__(self, message: ReplayWorkerMessage) -> None:
        self.busy = False
        self._message = message

    def poll(self) -> ReplayWorkerMessage | None:
        message = self._message
        self._message = None
        return message


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


class WindowsRecoveryOperatorJourneyTests(unittest.TestCase):
    def _app_after_replay_failure(self, workspace: Path) -> WindowsAutosportApp:
        app = object.__new__(WindowsAutosportApp)
        app._closing = False
        app.workspace = workspace
        app._active_workspace = workspace
        app._active_strategy_id = "baseline-v1"
        app._active_research_plan = None
        app._recovery_view = None
        app._recovery_blocked_workspace = None
        app._recovery_blocked_workspaces = set()
        app.dataset_worker = _BusyWorker(False)
        app.live_worker = _BusyWorker(False)
        app.recovery_worker = None
        app.replay_worker = _TerminalReplayWorker(
            ReplayWorkerMessage(
                error="RuntimeError: replay failed after durable work may have started"
            )
        )
        app.session = None
        app.status = _Value()
        app.bank = _Value()
        app.live_status = _Value()
        app._busy_states: list[bool] = []
        app._logs: list[str] = []
        app._evaluation: list[str] = []
        app._scheduled: list[tuple[int, str]] = []
        app._ticket_refreshes = 0
        app._set_replay_controls_busy = lambda busy: app._busy_states.append(busy)
        app._append_log = lambda message: app._logs.append(message)
        app._set_evaluation_lines = lambda lines: setattr(app, "_evaluation", list(lines))
        app._refresh_tickets = lambda: setattr(
            app, "_ticket_refreshes", app._ticket_refreshes + 1
        )
        app.after = lambda delay, callback: app._scheduled.append(
            (delay, callback.__name__)
        )
        app._selected_replay_configuration = lambda: ("baseline-v1", None)
        return app

    def test_replay_failure_repair_and_retry_form_one_fail_closed_operator_journey(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            app = self._app_after_replay_failure(workspace)

            with patch("autosport.windows_gui.messagebox.showerror") as replay_error:
                WindowsAutosportApp._poll_replay_worker(app)

            self.assertIsNone(app.session)
            self.assertIsNone(app._recovery_view)
            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertIn("прихованим до відновлення", app.status.value)
            replay_error.assert_called_once()

            with patch.object(AutosportApp, "run_dataset") as parent_run:
                WindowsAutosportApp.run_dataset(app)
            parent_run.assert_not_called()
            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertIn("заблоковано закрито при помилці", app.status.value)

            recovery_worker = _CapturedRecoveryWorker()
            app.recovery_worker = recovery_worker
            WindowsAutosportApp.repair_workspace(app)

            self.assertTrue(recovery_worker.busy)
            self.assertIsNotNone(recovery_worker.task)
            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertIsNone(app._recovery_view)
            self.assertIn("відновлення", app.status.value.lower())

            assert recovery_worker.task is not None
            recovery_result = recovery_worker.task()
            recovery_worker.message = RecoveryWorkerMessage(result=recovery_result)

            with patch("autosport.windows_gui.messagebox.showinfo") as recovery_info:
                WindowsAutosportApp._poll_recovery_worker(app)

            self.assertFalse(app._workspace_requires_recovery(workspace))
            self.assertIs(app._recovery_view, recovery_result.session_view)
            self.assertIsNone(app.session)
            self.assertEqual(recovery_result.session_view.workspace, workspace)
            self.assertEqual(recovery_result.session_view.strategy_id, "baseline-v1")
            self.assertIn("Робоча область готова", app.status.value)
            recovery_info.assert_called_once()

            with patch.object(AutosportApp, "run_dataset") as parent_run:
                WindowsAutosportApp.run_dataset(app)

            parent_run.assert_called_once_with()
            self.assertIsNone(app._recovery_view)
            self.assertFalse(app._workspace_requires_recovery(workspace))


    def test_foreign_recovery_result_never_unblocks_another_workspace(self) -> None:
        """A stale/misrouted worker response cannot release a quarantined workspace."""
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            foreign_workspace = workspace / "foreign-workspace"
            app = self._app_after_replay_failure(workspace)

            with patch("autosport.windows_gui.messagebox.showerror"):
                WindowsAutosportApp._poll_replay_worker(app)
            self.assertTrue(app._workspace_requires_recovery(workspace))
            app._block_workspace_for_recovery(foreign_workspace)

            recovery_worker = _CapturedRecoveryWorker()
            app.recovery_worker = recovery_worker
            WindowsAutosportApp.repair_workspace(app)
            self.assertIsNotNone(recovery_worker.task)
            assert recovery_worker.task is not None
            recovered = recovery_worker.task()
            foreign_result = replace(
                recovered,
                session_view=replace(
                    recovered.session_view, workspace=foreign_workspace
                ),
            )
            recovery_worker.message = RecoveryWorkerMessage(result=foreign_result)

            with patch("autosport.windows_gui.messagebox.showerror") as error_dialog:
                WindowsAutosportApp._poll_recovery_worker(app)

            error_dialog.assert_called_once()
            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertTrue(app._workspace_requires_recovery(foreign_workspace))
            self.assertIsNone(app._recovery_view)
            self.assertIsNone(app.session)

            with patch.object(AutosportApp, "run_dataset") as parent_run:
                WindowsAutosportApp.run_dataset(app)
            parent_run.assert_not_called()
            self.assertTrue(app._workspace_requires_recovery(workspace))

            # A later exact-identity repair releases only its own workspace.
            exact_worker = _CapturedRecoveryWorker()
            app.recovery_worker = exact_worker
            WindowsAutosportApp.repair_workspace(app)
            self.assertIsNotNone(exact_worker.task)
            assert exact_worker.task is not None
            exact_worker.message = RecoveryWorkerMessage(result=exact_worker.task())
            with patch("autosport.windows_gui.messagebox.showinfo") as info_dialog:
                WindowsAutosportApp._poll_recovery_worker(app)

            info_dialog.assert_called_once()
            self.assertFalse(app._workspace_requires_recovery(workspace))
            self.assertTrue(app._workspace_requires_recovery(foreign_workspace))
            self.assertIsNotNone(app._recovery_view)
            assert app._recovery_view is not None
            self.assertEqual(app._recovery_view.workspace, workspace)


    def test_wrong_strategy_recovery_result_never_unblocks_workspace(self) -> None:
        """The correct workspace is insufficient when a recovery reply has the wrong strategy.

        The worker task executes actual local recovery; only its result identity is
        substituted, to prove the Tk/operator acceptance boundary remains fail-closed.
        """
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            app = self._app_after_replay_failure(workspace)

            with patch("autosport.windows_gui.messagebox.showerror"):
                WindowsAutosportApp._poll_replay_worker(app)
            self.assertTrue(app._workspace_requires_recovery(workspace))

            worker = _CapturedRecoveryWorker()
            app.recovery_worker = worker
            WindowsAutosportApp.repair_workspace(app)
            self.assertIsNotNone(worker.task)
            assert worker.task is not None
            recovered = worker.task()
            wrong_strategy = replace(
                recovered,
                session_view=replace(
                    recovered.session_view,
                    strategy_id="foreign-strategy-v2",
                ),
            )
            worker.message = RecoveryWorkerMessage(result=wrong_strategy)

            with patch("autosport.windows_gui.messagebox.showerror") as error_dialog:
                WindowsAutosportApp._poll_recovery_worker(app)
            error_dialog.assert_called_once()
            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertIsNone(app._recovery_view)
            self.assertIsNone(app.session)
            self.assertIn("не відповідає", app.status.value.lower())

            with patch.object(AutosportApp, "run_dataset") as parent_run:
                WindowsAutosportApp.run_dataset(app)
            parent_run.assert_not_called()

            # A later exact workspace + strategy match, and only that match,
            # can release the quarantine without a second replay side effect.
            valid_worker = _CapturedRecoveryWorker()
            app.recovery_worker = valid_worker
            WindowsAutosportApp.repair_workspace(app)
            assert valid_worker.task is not None
            valid_worker.message = RecoveryWorkerMessage(result=valid_worker.task())
            with patch("autosport.windows_gui.messagebox.showinfo") as info_dialog:
                WindowsAutosportApp._poll_recovery_worker(app)
            info_dialog.assert_called_once()
            self.assertFalse(app._workspace_requires_recovery(workspace))
            self.assertEqual(app._recovery_view.strategy_id, "baseline-v1")


if __name__ == "__main__":
    unittest.main()
