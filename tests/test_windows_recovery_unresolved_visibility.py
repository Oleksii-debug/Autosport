from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from autosport.recovery import RecoveryReport
from autosport.recovery_worker import RecoveryTaskResult, RecoveryWorkerMessage
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


class WindowsRecoveryUnresolvedVisibilityTests(unittest.TestCase):
    def test_unresolved_recovery_keeps_economic_state_hidden_and_quarantined(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            view = SimpleNamespace(
                workspace=workspace,
                strategy_id="baseline-v1",
                book=SimpleNamespace(balance=123, committed_stake=45),
            )
            result = RecoveryTaskResult(
                report=RecoveryReport((), (), ("legacy-unresolved",)),
                session_view=view,
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
            app.session = None
            app.bank = _Value()
            app.status = _Value()
            app._logs = []
            app._visible_recovery_views = []
            app._set_replay_controls_busy = lambda _busy: None
            app._append_log = lambda text: app._logs.append(text)
            app._bank_text = lambda: (
                "hidden" if app._recovery_view is None and app.session is None else "EXPOSED"
            )
            app._refresh_tickets = lambda: app._visible_recovery_views.append(app._recovery_view)

            with (
                patch("autosport.windows_gui.messagebox.showwarning") as warning,
                patch("autosport.windows_gui.messagebox.showinfo") as info,
            ):
                WindowsAutosportApp._poll_recovery_worker(app)

            self.assertIsNone(app._recovery_view)
            self.assertIsNone(app.session)
            self.assertEqual(app.bank.value, "hidden")
            self.assertEqual(app._visible_recovery_views, [None])
            self.assertIn(workspace, app._recovery_blocked_workspaces)
            self.assertEqual(app._recovery_blocked_workspace, workspace)
            self.assertIn("economic state лишається прихованим", app.status.value)
            self.assertTrue(any("unresolved=1" in line for line in app._logs))
            warning.assert_called_once()
            info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
