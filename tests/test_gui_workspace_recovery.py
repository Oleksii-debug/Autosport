from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace

from autosport.gui import AUTOMATION_IDS, AutosportApp


class _Var:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _Tickets:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def delete(self, _start, _end) -> None:
        self.lines.clear()

    def insert(self, _where, value: str) -> None:
        self.lines.append(value)


class _Worker:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.task = None

    def start(self, task) -> bool:
        if self.busy:
            return False
        self.task = task
        self.busy = True
        return True


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class GuiWorkspaceRecoveryTests(unittest.TestCase):
    def test_gui_wires_recovery_to_background_worker_keyboard_and_uia(self) -> None:
        build_source = inspect.getsource(AutosportApp._build)
        accessibility_source = inspect.getsource(AutosportApp._configure_accessibility)
        recovery_source = inspect.getsource(AutosportApp.repair_workspace)
        poll_source = inspect.getsource(AutosportApp._poll_recovery_worker)
        busy_source = inspect.getsource(AutosportApp._set_replay_controls_busy)
        close_source = inspect.getsource(AutosportApp.close_app)

        self.assertEqual(AUTOMATION_IDS["repair_workspace"], 108)
        self.assertIn("self.repair_button = ttk.Button", build_source)
        self.assertIn('self.bind("<Control-Shift-R>"', build_source)
        self.assertIn('"Відновити workspace"', accessibility_source)
        self.assertIn('AUTOMATION_IDS["repair_workspace"]', accessibility_source)
        self.assertIn("self.recovery_worker.start(task)", recovery_source)
        self.assertIn("recover_workspace_once(", recovery_source)
        self.assertIn("self.after(100, self._poll_recovery_worker)", recovery_source)
        self.assertNotIn("reconcile_late_crashes", recovery_source)
        self.assertIn("self.recovery_worker.poll()", poll_source)
        self.assertIn("report.unresolved_without_summary", poll_source)
        self.assertIn("allow-repeat", poll_source)
        self.assertIn('self.repair_button.state(["disabled"])', busy_source)
        self.assertIn("if self.recovery_worker.busy", close_source)

    def test_repair_workspace_returns_after_scheduling_worker(self) -> None:
        recovery_worker = _Worker()
        session = _Session()
        scheduled: list[tuple[int, object]] = []
        busy_states: list[bool] = []
        logs: list[str] = []
        status = _Var()
        bank = _Var()
        tickets = _Tickets()

        fake = SimpleNamespace(
            _closing=False,
            recovery_worker=recovery_worker,
            replay_worker=_Worker(),
            live_worker=_Worker(),
            workspace=Path("workspace"),
            session=session,
            status=status,
            bank=bank,
            tickets=tickets,
            _selected_replay_configuration=lambda: ("baseline-v1", None),
            _bank_text=lambda: "bank-updating",
            _set_replay_controls_busy=lambda value: busy_states.append(value),
            _append_log=logs.append,
            after=lambda delay, callback: scheduled.append((delay, callback)),
            _poll_recovery_worker=lambda: None,
        )

        AutosportApp.repair_workspace(fake)

        self.assertTrue(session.closed)
        self.assertIsNone(fake.session)
        self.assertTrue(recovery_worker.busy)
        self.assertIsNotNone(recovery_worker.task)
        self.assertEqual(busy_states, [True])
        self.assertEqual(bank.value, "bank-updating")
        self.assertIn("Workspace recovery виконується", tickets.lines[0])
        self.assertIn("background worker", status.value)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], 100)
        self.assertTrue(logs)

    def test_close_is_blocked_while_recovery_is_active(self) -> None:
        status = _Var()
        logs: list[str] = []
        bells: list[bool] = []
        destroyed: list[bool] = []
        session = _Session()
        fake = SimpleNamespace(
            recovery_worker=_Worker(busy=True),
            replay_worker=_Worker(),
            status=status,
            _append_log=logs.append,
            bell=lambda: bells.append(True),
            _closing=False,
            session=session,
            destroy=lambda: destroyed.append(True),
        )

        AutosportApp.close_app(fake)

        self.assertIn("Workspace recovery ще виконується", status.value)
        self.assertEqual(bells, [True])
        self.assertEqual(destroyed, [])
        self.assertFalse(session.closed)
        self.assertFalse(fake._closing)

    def test_recovery_busy_guard_exists_on_conflicting_product_paths(self) -> None:
        for method in (
            AutosportApp._on_strategy_changed,
            AutosportApp.choose_research_plan,
            AutosportApp.choose_dataset,
            AutosportApp.refresh_live_snapshot,
            AutosportApp.run_dataset,
            AutosportApp.close_app,
        ):
            self.assertIn(
                "self.recovery_worker.busy",
                inspect.getsource(method),
                method.__name__,
            )


if __name__ == "__main__":
    unittest.main()
