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


class _IdleRecoveryWorker:
    def __init__(self) -> None:
        self.busy = False
        self.started = False

    def start(self, _task) -> bool:
        self.started = True
        return True


class WindowsRecoveryTeardownFailureTests(unittest.TestCase):
    def _app(self, root: Path, prior_workspace: Path) -> WindowsAutosportApp:
        app = object.__new__(WindowsAutosportApp)
        app._closing = False
        app.workspace = root
        app._active_workspace = prior_workspace
        app._active_strategy_id = "baseline-v1"
        app._active_research_plan = None
        app._recovery_view = None
        app._recovery_blocked_workspace = None
        app._recovery_blocked_workspaces = set()
        app.replay_worker = SimpleNamespace(busy=False)
        app.live_worker = SimpleNamespace(busy=False)
        app.recovery_worker = _IdleRecoveryWorker()
        app.status = _Value()
        app.bank = _Value()
        app._logs = []
        app._ticket_sessions = []
        app._selected_replay_configuration = lambda: ("research-v1", None)
        app._append_log = lambda text: app._logs.append(text)
        app._bank_text = lambda: (
            "hidden"
            if app.session is None and app._recovery_view is None
            else "EXPOSED"
        )
        app._refresh_tickets = lambda: app._ticket_sessions.append(app.session)
        app._set_replay_controls_busy = lambda _busy: self.fail(
            "recovery controls must not enter busy state after teardown failure"
        )
        app.after = lambda *_args: self.fail(
            "recovery polling must not be scheduled after teardown failure"
        )
        return app

    def test_teardown_failure_detaches_state_and_blocks_both_workspaces(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            prior_workspace = root / "baseline-workspace"
            selected_workspace = root / "selected-workspace"
            app = self._app(root, prior_workspace)

            detached_before_close: list[bool] = []

            class _FailingSession:
                # Recovery teardown must not require a new session.workspace protocol.
                def close(self) -> None:
                    detached_before_close.append(app.session is None)
                    raise RuntimeError("teardown exploded")

            app.session = _FailingSession()

            with (
                patch("autosport.windows_gui.workspace_for_strategy", return_value=selected_workspace),
                patch("autosport.windows_gui.messagebox.showerror") as error,
            ):
                WindowsAutosportApp.repair_workspace(app)

            self.assertEqual(detached_before_close, [True])
            self.assertIsNone(app.session)
            self.assertFalse(app.recovery_worker.started)
            self.assertEqual(app._active_workspace, selected_workspace)
            self.assertEqual(app._recovery_blocked_workspace, prior_workspace)
            self.assertEqual(
                app._recovery_blocked_workspaces,
                {prior_workspace, selected_workspace},
            )
            self.assertTrue(app._workspace_requires_recovery(prior_workspace))
            self.assertTrue(app._workspace_requires_recovery(selected_workspace))
            self.assertEqual(app.bank.value, "hidden")
            self.assertEqual(app._ticket_sessions, [None])
            self.assertIn("не вдалося завершити попередній економічний сеанс", app.status.value)
            self.assertTrue(
                any("RuntimeError: teardown exploded" in line for line in app._logs),
                app._logs,
            )
            error.assert_called_once()
            self.assertIn("не вдалося завершити попередній економічний сеанс", error.call_args.args[1])

            # Recovering the newly selected target later must not silently clear
            # the independent quarantine for the prior session whose teardown was
            # never proven successful.
            app._unblock_workspace_after_recovery(selected_workspace)
            self.assertFalse(app._workspace_requires_recovery(selected_workspace))
            self.assertTrue(app._workspace_requires_recovery(prior_workspace))

    def test_process_control_teardown_propagates_after_exact_session_workspace_quarantine(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale_active_workspace = root / "stale-active-workspace"
            exact_session_workspace = root / "actual-attached-workspace"
            selected_workspace = root / "selected-workspace"
            app = self._app(root, stale_active_workspace)

            class _InterruptedSession:
                def __init__(self) -> None:
                    self.workspace = exact_session_workspace

                def close(self) -> None:
                    self.assert_detached()
                    raise KeyboardInterrupt("teardown interrupted")

                @staticmethod
                def assert_detached() -> None:
                    if app.session is not None:
                        raise AssertionError("economic session must detach before teardown")

            app.session = _InterruptedSession()

            with (
                patch("autosport.windows_gui.workspace_for_strategy", return_value=selected_workspace),
                patch("autosport.windows_gui.messagebox.showerror") as error,
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "teardown interrupted"):
                    WindowsAutosportApp.repair_workspace(app)

            self.assertIsNone(app.session)
            self.assertFalse(app.recovery_worker.started)
            self.assertEqual(app._active_workspace, selected_workspace)
            self.assertEqual(app._recovery_blocked_workspace, exact_session_workspace)
            self.assertEqual(
                app._recovery_blocked_workspaces,
                {exact_session_workspace, selected_workspace},
            )
            self.assertNotIn(stale_active_workspace, app._recovery_blocked_workspaces)
            self.assertTrue(app._workspace_requires_recovery(exact_session_workspace))
            self.assertTrue(app._workspace_requires_recovery(selected_workspace))
            self.assertEqual(app.bank.value, "hidden")
            self.assertEqual(app._ticket_sessions, [None])
            error.assert_not_called()

    def test_hostile_teardown_exception_metadata_cannot_escape_fail_closed_handler(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            prior_workspace = root / "baseline-workspace"
            selected_workspace = root / "selected-workspace"
            app = self._app(root, prior_workspace)

            class _BrokenNameMeta(type):
                def __getattribute__(cls, name: str):
                    if name == "__name__":
                        raise RuntimeError("broken exception type formatter")
                    return super().__getattribute__(name)

            class _HostileTeardownError(Exception, metaclass=_BrokenNameMeta):
                def __str__(self) -> str:
                    raise RuntimeError("broken exception formatter")

            class _FailingSession:
                def close(self) -> None:
                    self_detached = app.session is None
                    if not self_detached:
                        raise AssertionError("economic session must detach before teardown")
                    raise _HostileTeardownError()

            app.session = _FailingSession()

            with (
                patch("autosport.windows_gui.workspace_for_strategy", return_value=selected_workspace),
                patch("autosport.windows_gui.messagebox.showerror") as error,
            ):
                WindowsAutosportApp.repair_workspace(app)

            self.assertIsNone(app.session)
            self.assertFalse(app.recovery_worker.started)
            self.assertEqual(
                app._recovery_blocked_workspaces,
                {prior_workspace, selected_workspace},
            )
            self.assertTrue(app._workspace_requires_recovery(prior_workspace))
            self.assertTrue(app._workspace_requires_recovery(selected_workspace))
            self.assertEqual(app.bank.value, "hidden")
            self.assertEqual(app._ticket_sessions, [None])
            self.assertIn("не вдалося завершити попередній економічний сеанс", app.status.value)
            fallback = "Exception: <повідомлення недоступне>"
            self.assertTrue(any(fallback in line for line in app._logs), app._logs)
            error.assert_called_once()
            self.assertIn(fallback, error.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
