from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autosport.replay_worker import ReplayWorkerMessage
from autosport.windows_gui import WindowsAutosportApp


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _TerminalReplayWorker:
    def __init__(self, message: ReplayWorkerMessage) -> None:
        self.busy = False
        self._message = message

    def poll(self):
        message = self._message
        self._message = None
        return message


class WindowsReplayRecoveryGateTests(unittest.TestCase):
    def _app(self, workspace: Path, message: ReplayWorkerMessage) -> WindowsAutosportApp:
        app = object.__new__(WindowsAutosportApp)
        app.workspace = workspace
        app._active_workspace = workspace
        app._active_strategy_id = "baseline-v1"
        app._active_research_plan = None
        app._recovery_view = None
        app._recovery_blocked_workspace = None
        app._recovery_blocked_workspaces = set()
        app.recovery_worker = None
        app.replay_worker = _TerminalReplayWorker(message)
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

    def test_replay_error_quarantines_workspace_until_recovery(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            app = self._app(
                workspace,
                ReplayWorkerMessage(error="RuntimeError: replay failed after durable work may have started"),
            )
            app._open_session = lambda *_args, **_kwargs: object()

            with patch("autosport.windows_gui.messagebox.showerror") as error:
                WindowsAutosportApp._poll_replay_worker(app)

            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertEqual(app._recovery_blocked_workspace, workspace)
            self.assertIn("заблоковано fail-closed", app.status.value)
            self.assertEqual(app._busy_states, [False])
            error.assert_called_once()

            WindowsAutosportApp.run_dataset(app)
            self.assertIn("заблоковано fail-closed", app.status.value)

    def test_post_replay_reopen_failure_is_user_visible_and_quarantines_workspace(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            app = self._app(workspace, ReplayWorkerMessage(result=object()))
            app._open_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("workspace state failed validation")
            )

            with patch("autosport.windows_gui.messagebox.showerror") as error:
                WindowsAutosportApp._poll_replay_worker(app)

            self.assertIsNone(app.session)
            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertIn("terminal state не можна безпечно підтвердити", app.status.value)
            self.assertIn("post-replay workspace reopen", app._evaluation[0])
            self.assertTrue(any("workspace state failed validation" in line for line in app._logs))
            error.assert_called_once()

    def test_multiple_failed_strategy_workspaces_remain_independently_quarantined(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "strategies" / "first"
            second = root / "strategies" / "second"
            app = object.__new__(WindowsAutosportApp)
            app._recovery_blocked_workspace = None
            app._recovery_blocked_workspaces = set()

            app._block_workspace_for_recovery(first)
            app._block_workspace_for_recovery(second)
            self.assertTrue(app._workspace_requires_recovery(first))
            self.assertTrue(app._workspace_requires_recovery(second))

            app._unblock_workspace_after_recovery(second)
            self.assertTrue(app._workspace_requires_recovery(first))
            self.assertFalse(app._workspace_requires_recovery(second))

    def test_legacy_single_blocked_workspace_is_migrated_into_quarantine_set(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            app = object.__new__(WindowsAutosportApp)
            app._recovery_blocked_workspace = workspace

            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertEqual(app._recovery_blocked_workspaces, {workspace})


if __name__ == "__main__":
    unittest.main()
