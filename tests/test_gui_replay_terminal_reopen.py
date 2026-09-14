from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.gui import AutosportApp
from autosport.replay_worker import ReplayWorkerMessage


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _Tickets:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def delete(self, _start, _end) -> None:
        self.lines.clear()

    def insert(self, _index, value: str) -> None:
        self.lines.append(value)


class _TerminalWorker:
    def __init__(self, message) -> None:
        self._message = message
        self.busy = False

    def poll(self):
        message = self._message
        self._message = None
        return message


class _RunWorker:
    def __init__(self) -> None:
        self.busy = False
        self.start_calls = 0

    def start(self, _task) -> bool:
        self.start_calls += 1
        return True


class _Session:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.strategy_id = "baseline-v1"
        self.book = SimpleNamespace(balance=10000, committed_stake=0)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _partial_app(message) -> AutosportApp:
    app = object.__new__(AutosportApp)
    app.replay_worker = _TerminalWorker(message)
    app.live_worker = SimpleNamespace(busy=False)
    app.workspace = Path("root-workspace")
    app._active_strategy_id = "baseline-v1"
    app._active_research_plan = None
    app._active_workspace = Path("economic-workspace")
    app._recovery_required_workspaces = set()
    app._closing = False
    app.session = None
    app.dataset_path = None
    app.status = _Value()
    app.bank = _Value()
    app.speed_text = _Value("Подієвий — максимально швидко")
    app.tickets = _Tickets()
    app._busy_states = []
    app._logs = []
    app._evaluation = []
    app._set_replay_controls_busy = lambda busy: app._busy_states.append(busy)
    app._append_log = lambda text: app._logs.append(text)
    app._set_evaluation_lines = lambda lines: setattr(app, "_evaluation", list(lines))
    app.after = lambda *_args: (_ for _ in ()).throw(
        AssertionError("terminal message must not reschedule polling")
    )
    return app


def test_terminal_replay_reopen_failure_is_contained_and_requires_recovery() -> None:
    app = _partial_app(ReplayWorkerMessage(result=object()))
    app._open_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ValueError("workspace state failed validation")
    )

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_replay_worker(app)

    assert app.session is None
    assert app._busy_states == [False]
    assert app.bank.value.startswith("Віртуальний банк: оновлюється після replay")
    assert app.tickets.lines == [
        "Replay завершено, але economic session state недоступний; виконайте recovery workspace."
    ]
    assert app._evaluation == [
        "Evaluation недоступна: post-replay workspace reopen не пройшов fail-closed validation."
    ]
    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert "terminal state не можна безпечно підтвердити" in app.status.value
    assert any("workspace state failed validation" in line for line in app._logs)
    showerror.assert_called_once()


def test_replay_error_context_survives_secondary_reopen_failure_and_requires_recovery() -> None:
    app = _partial_app(ReplayWorkerMessage(error="RuntimeError: replay failed"))
    app._open_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ValueError("workspace state failed validation")
    )

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_replay_worker(app)

    detail = app._logs[-1]
    assert "Paper replay помилка: RuntimeError: replay failed" in detail
    assert "Post-replay workspace reopen" in detail
    assert "workspace state failed validation" in detail
    assert Path("economic-workspace") in app._recovery_required_workspaces
    showerror.assert_called_once()


def test_replay_error_requires_recovery_even_when_session_reopens() -> None:
    app = _partial_app(ReplayWorkerMessage(error="RuntimeError: replay failed"))
    app._open_session = lambda *_args, **_kwargs: _Session(Path("economic-workspace"))
    app._refresh_tickets = lambda: None

    with patch("autosport.gui.messagebox.showerror"):
        AutosportApp._poll_replay_worker(app)

    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert "механічно заблоковано до recovery" in app.status.value


def test_missing_terminal_result_requires_recovery() -> None:
    app = _partial_app(SimpleNamespace(result=None, error=None))
    app._open_session = lambda *_args, **_kwargs: _Session(Path("economic-workspace"))
    app._refresh_tickets = lambda: None

    AutosportApp._poll_replay_worker(app)

    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert "механічно заблоковано" in app.status.value


def test_second_run_is_blocked_until_successful_exact_workspace_recovery() -> None:
    app = _partial_app(ReplayWorkerMessage(error="unused"))
    run_worker = _RunWorker()
    app.replay_worker = run_worker
    app.dataset_path = Path("dataset")
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._recovery_required_workspaces = {
        Path("economic-workspace"),
        Path("other-economic-workspace"),
    }
    app._bank_text = lambda: "bank"
    app._refresh_tickets = lambda: None
    app.after = lambda *_args: None

    with (
        patch(
            "autosport.gui.workspace_for_strategy",
            return_value=Path("economic-workspace"),
        ),
        patch("autosport.gui.messagebox.showwarning") as showwarning,
    ):
        AutosportApp.run_dataset(app)

    assert run_worker.start_calls == 0
    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert "Paper replay заблоковано" in app.status.value
    showwarning.assert_called_once()

    clean_report = SimpleNamespace(
        reconciled_keys=(),
        aborted_uncommitted_keys=(),
        unresolved_without_summary=(),
    )
    reopened_session = _Session(Path("economic-workspace"))
    app._open_session = lambda *_args, **_kwargs: reopened_session

    with (
        patch(
            "autosport.gui.workspace_for_strategy",
            return_value=Path("economic-workspace"),
        ),
        patch("autosport.gui.reconcile_late_crashes", return_value=clean_report),
        patch("autosport.gui.messagebox.showinfo") as showinfo,
    ):
        AutosportApp.repair_workspace(app)

    assert Path("economic-workspace") not in app._recovery_required_workspaces
    assert Path("other-economic-workspace") in app._recovery_required_workspaces
    showinfo.assert_called_once()

    with patch(
        "autosport.gui.workspace_for_strategy",
        return_value=Path("economic-workspace"),
    ):
        AutosportApp.run_dataset(app)

    assert reopened_session.closed
    assert run_worker.start_calls == 1


def test_unresolved_recovery_keeps_exact_workspace_blocked() -> None:
    app = _partial_app(ReplayWorkerMessage(error="unused"))
    app.replay_worker = _RunWorker()
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._recovery_required_workspaces = {Path("economic-workspace")}
    app._bank_text = lambda: "bank"
    app._refresh_tickets = lambda: None
    app._open_session = lambda *_args, **_kwargs: _Session(Path("economic-workspace"))
    unresolved_report = SimpleNamespace(
        reconciled_keys=(),
        aborted_uncommitted_keys=(),
        unresolved_without_summary=("run-1",),
    )

    with (
        patch(
            "autosport.gui.workspace_for_strategy",
            return_value=Path("economic-workspace"),
        ),
        patch("autosport.gui.reconcile_late_crashes", return_value=unresolved_report),
        patch("autosport.gui.messagebox.showwarning") as showwarning,
    ):
        AutosportApp.repair_workspace(app)

    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert "лишається fail-closed" in app.status.value
    showwarning.assert_called_once()
