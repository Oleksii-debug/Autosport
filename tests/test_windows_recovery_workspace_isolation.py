from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from autosport.gui import AutosportApp
from autosport.recovery import RecoveryReport
from autosport.recovery_worker import RecoveryTaskResult, RecoveryWorkerMessage
from autosport.replay_worker import ReplayWorkerMessage
from autosport.windows_gui import WindowsAutosportApp


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _TerminalWorker:
    def __init__(self, message) -> None:
        self.busy = False
        self._message = message

    def poll(self):
        message = self._message
        self._message = None
        return message


class WindowsRecoveryWorkspaceIsolationTests(unittest.TestCase):
    def _replay_app(self, workspace: Path, message) -> WindowsAutosportApp:
        app = object.__new__(WindowsAutosportApp)
        app.workspace = workspace
        app._active_workspace = workspace
        app._active_strategy_id = "baseline-v1"
        app._active_research_plan = None
        app._recovery_view = None
        app._recovery_blocked_workspace = None
        app._recovery_blocked_workspaces = set()
        app.recovery_worker = None
        app.replay_worker = _TerminalWorker(message)
        app.session = None
        app.status = _Value()
        app.bank = _Value()
        app._busy_states = []
        app._logs = []
        app._evaluation = []
        app._ticket_refreshes = 0
        app._set_replay_controls_busy = lambda busy: app._busy_states.append(busy)
        app._append_log = lambda text: app._logs.append(text)
        app._set_evaluation_lines = lambda lines: setattr(app, "_evaluation", list(lines))
        app._refresh_tickets = lambda: setattr(app, "_ticket_refreshes", app._ticket_refreshes + 1)
        app._bank_text = lambda: "bank"
        app.after = lambda *_args: self.fail("terminal message must not reschedule polling")
        app._selected_replay_configuration = lambda: ("baseline-v1", None)
        return app

    def test_success_for_workspace_b_does_not_unblock_failed_workspace_a(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_a = root
            workspace_b = root / "strategies" / "strategy-b"
            view_b = SimpleNamespace(
                workspace=workspace_b,
                strategy_id="strategy-b",
                book=SimpleNamespace(balance=0, committed_stake=0),
            )
            result_b = RecoveryTaskResult(
                report=RecoveryReport((), (), ()),
                session_view=view_b,
            )

            app = object.__new__(WindowsAutosportApp)
            app.workspace = root
            app._active_workspace = workspace_b
            app._active_strategy_id = "strategy-b"
            app._active_research_plan = None
            app._recovery_view = None
            app._recovery_blocked_workspace = workspace_b
            app._recovery_blocked_workspaces = {workspace_a, workspace_b}
            app.recovery_worker = _TerminalWorker(RecoveryWorkerMessage(result=result_b))
            app.bank = _Value()
            app.status = _Value()
            app._set_replay_controls_busy = lambda _busy: None
            app._refresh_tickets = lambda: None
            app._append_log = lambda _text: None
            app._bank_text = lambda: "bank"

            with patch("autosport.windows_gui.messagebox.showinfo"):
                WindowsAutosportApp._poll_recovery_worker(app)

            self.assertIn(workspace_a, app._recovery_blocked_workspaces)
            self.assertNotIn(workspace_b, app._recovery_blocked_workspaces)
            self.assertIsNone(app._recovery_blocked_workspace)

            app._selected_replay_configuration = lambda: ("baseline-v1", None)
            with patch.object(AutosportApp, "run_dataset") as parent_run:
                WindowsAutosportApp.run_dataset(app)

            parent_run.assert_not_called()
            self.assertIn("заблоковано fail-closed", app.status.value)

    def test_mismatched_terminal_workspace_never_unblocks_returned_workspace(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace_a = root / "strategies" / "strategy-a"
            workspace_b = root / "strategies" / "strategy-b"
            result_b = RecoveryTaskResult(
                report=RecoveryReport((), (), ()),
                session_view=SimpleNamespace(
                    workspace=workspace_b,
                    strategy_id="strategy-b",
                    book=SimpleNamespace(balance=0, committed_stake=0),
                ),
            )

            app = object.__new__(WindowsAutosportApp)
            app.workspace = root
            app._active_workspace = workspace_a
            app._active_strategy_id = "strategy-a"
            app._active_research_plan = None
            app._recovery_view = None
            app._recovery_blocked_workspace = workspace_a
            app._recovery_blocked_workspaces = {workspace_a, workspace_b}
            app.recovery_worker = _TerminalWorker(RecoveryWorkerMessage(result=result_b))
            app.bank = _Value()
            app.status = _Value()
            app._logs = []
            app._set_replay_controls_busy = lambda _busy: None
            app._refresh_tickets = lambda: None
            app._append_log = lambda text: app._logs.append(text)
            app._bank_text = lambda: "bank"

            with (
                patch("autosport.windows_gui.messagebox.showerror") as error,
                patch("autosport.windows_gui.messagebox.showinfo") as info,
            ):
                WindowsAutosportApp._poll_recovery_worker(app)

            self.assertIsNone(app._recovery_view)
            self.assertEqual(app._recovery_blocked_workspace, workspace_a)
            self.assertIn(workspace_a, app._recovery_blocked_workspaces)
            self.assertIn(workspace_b, app._recovery_blocked_workspaces)
            self.assertIn("terminal result не відповідає", app.status.value)
            self.assertTrue(any("terminal identity mismatch" in line for line in app._logs))
            error.assert_called_once()
            info.assert_not_called()

    def test_mismatched_terminal_strategy_keeps_requested_workspace_blocked(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            result = RecoveryTaskResult(
                report=RecoveryReport((), (), ()),
                session_view=SimpleNamespace(
                    workspace=workspace,
                    strategy_id="wrong-strategy",
                    book=SimpleNamespace(balance=0, committed_stake=0),
                ),
            )

            app = object.__new__(WindowsAutosportApp)
            app.workspace = workspace
            app._active_workspace = workspace
            app._active_strategy_id = "baseline-v1"
            app._active_research_plan = None
            app._recovery_view = None
            app._recovery_blocked_workspace = workspace
            app._recovery_blocked_workspaces = {workspace}
            app.recovery_worker = _TerminalWorker(RecoveryWorkerMessage(result=result))
            app.bank = _Value()
            app.status = _Value()
            app._logs = []
            app._set_replay_controls_busy = lambda _busy: None
            app._refresh_tickets = lambda: None
            app._append_log = lambda text: app._logs.append(text)
            app._bank_text = lambda: "bank"

            with patch("autosport.windows_gui.messagebox.showerror") as error:
                WindowsAutosportApp._poll_recovery_worker(app)

            self.assertIsNone(app._recovery_view)
            self.assertEqual(app._recovery_blocked_workspace, workspace)
            self.assertEqual(app._recovery_blocked_workspaces, {workspace})
            self.assertIn("terminal result не відповідає", app.status.value)
            self.assertTrue(any("wrong-strategy" in line for line in app._logs))
            error.assert_called_once()

    def test_replay_error_quarantines_workspace_until_successful_recovery(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            app = self._replay_app(
                workspace,
                ReplayWorkerMessage(error="RuntimeError: replay failed after durable work may have started"),
            )
            app._open_session = lambda *_args, **_kwargs: object()

            with patch("autosport.windows_gui.messagebox.showerror") as error:
                WindowsAutosportApp._poll_replay_worker(app)

            self.assertIn(workspace, app._recovery_blocked_workspaces)
            self.assertEqual(app._recovery_blocked_workspace, workspace)
            self.assertIn("заблоковано fail-closed", app.status.value)
            self.assertEqual(app._busy_states, [False])
            error.assert_called_once()

            with patch.object(AutosportApp, "run_dataset") as parent_run:
                WindowsAutosportApp.run_dataset(app)
            parent_run.assert_not_called()
            self.assertIn("заблоковано fail-closed", app.status.value)

    def test_missing_terminal_replay_result_quarantines_workspace(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            # ReplayWorkerMessage itself forbids an empty terminal payload. This
            # lightweight terminal-like object exercises the GUI's defensive path
            # if a future/alternate worker violates that contract.
            app = self._replay_app(workspace, SimpleNamespace(result=None, error=None))
            app._open_session = lambda *_args, **_kwargs: object()

            WindowsAutosportApp._poll_replay_worker(app)

            self.assertIn(workspace, app._recovery_blocked_workspaces)
            self.assertEqual(app._recovery_blocked_workspace, workspace)
            self.assertIn("без terminal result", app.status.value)
            self.assertIn("worker не повернув terminal", app._evaluation[0])

    def test_post_replay_reopen_failure_is_user_visible_and_quarantines_workspace(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            app = self._replay_app(workspace, ReplayWorkerMessage(result=object()))
            app._open_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("workspace state failed validation")
            )

            with patch("autosport.windows_gui.messagebox.showerror") as error:
                WindowsAutosportApp._poll_replay_worker(app)

            self.assertIsNone(app.session)
            self.assertIn(workspace, app._recovery_blocked_workspaces)
            self.assertEqual(app._recovery_blocked_workspace, workspace)
            self.assertIn("terminal state не можна безпечно підтвердити", app.status.value)
            self.assertIn("post-replay workspace reopen", app._evaluation[0])
            self.assertTrue(any("workspace state failed validation" in line for line in app._logs))
            error.assert_called_once()

    def test_legacy_single_blocked_workspace_is_migrated_into_quarantine_set(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            app = object.__new__(WindowsAutosportApp)
            app._recovery_blocked_workspace = workspace

            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertEqual(app._recovery_blocked_workspaces, {workspace})


if __name__ == "__main__":
    unittest.main()
