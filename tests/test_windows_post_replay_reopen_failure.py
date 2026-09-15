from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from autosport.windows_gui import WindowsAutosportApp


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class WindowsPostReplayReopenFailureTests(unittest.TestCase):
    def _app(self, workspace: Path) -> WindowsAutosportApp:
        app = object.__new__(WindowsAutosportApp)
        app._active_workspace = workspace
        app._active_strategy_id = "baseline-v1"
        app._active_research_plan = None
        app._recovery_view = object()
        app._recovery_blocked_workspace = None
        app._recovery_blocked_workspaces = set()
        app.session = object()
        app.replay_worker = SimpleNamespace(
            poll=lambda: SimpleNamespace(error=None, result=object())
        )
        app.status = _Value()
        app.bank = _Value()
        app._logs: list[str] = []
        app._ticket_sessions: list[object | None] = []
        app._evaluation_lines: list[str] = []
        app._set_replay_controls_busy = lambda busy: self.assertFalse(busy)
        app._append_log = lambda text: app._logs.append(text)
        app._refresh_tickets = lambda: app._ticket_sessions.append(app.session)
        app._set_evaluation_lines = lambda lines: setattr(
            app, "_evaluation_lines", list(lines)
        )
        return app

    def test_hostile_reopen_exception_cannot_escape_fail_closed_quarantine(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "economic-workspace"
            app = self._app(workspace)

            class _BrokenNameMeta(type):
                def __getattribute__(cls, name: str):
                    if name == "__name__":
                        raise RuntimeError("broken exception type formatter")
                    return super().__getattribute__(name)

            class _HostileReopenError(Exception, metaclass=_BrokenNameMeta):
                def __str__(self) -> str:
                    raise RuntimeError("broken exception formatter")

            def fail_reopen(_strategy_id, _research_plan):
                raise _HostileReopenError()

            app._open_session = fail_reopen

            with patch("autosport.windows_gui.messagebox.showerror") as error:
                WindowsAutosportApp._poll_replay_worker(app)

            self.assertIsNone(app.session)
            self.assertIsNone(app._recovery_view)
            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertEqual(app._recovery_blocked_workspaces, {workspace})
            self.assertIn("оновлюється після replay", app.bank.value)
            self.assertEqual(app._ticket_sessions, [None])
            self.assertEqual(
                app._evaluation_lines,
                [
                    "Evaluation недоступна: post-replay workspace reopen не пройшов fail-closed validation."
                ],
            )
            self.assertIn("заблоковано fail-closed", app.status.value)
            fallback = "_HostileReopenError: exception details unavailable"
            self.assertTrue(any(fallback in line for line in app._logs), app._logs)
            error.assert_called_once()
            self.assertIn(fallback, error.call_args.args[1])

    def test_process_control_reopen_propagates_after_fail_closed_quarantine(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "economic-workspace"
            app = self._app(workspace)

            def interrupt_reopen(_strategy_id, _research_plan):
                raise KeyboardInterrupt("reopen interrupted")

            app._open_session = interrupt_reopen

            with patch("autosport.windows_gui.messagebox.showerror") as error:
                with self.assertRaisesRegex(KeyboardInterrupt, "reopen interrupted"):
                    WindowsAutosportApp._poll_replay_worker(app)

            self.assertIsNone(app.session)
            self.assertIsNone(app._recovery_view)
            self.assertTrue(app._workspace_requires_recovery(workspace))
            self.assertEqual(app._recovery_blocked_workspaces, {workspace})
            self.assertIn("оновлюється після replay", app.bank.value)
            self.assertEqual(app._ticket_sessions, [None])
            self.assertEqual(
                app._evaluation_lines,
                [
                    "Evaluation недоступна: post-replay workspace reopen не пройшов fail-closed validation."
                ],
            )
            error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
