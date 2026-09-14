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


def _seed_visible_economic_state(app: AutosportApp) -> _Session:
    stale = _Session(Path("economic-workspace"))
    app.session = stale
    app.bank.set("STALE BANKROLL=9999")
    app.tickets.lines = ["STALE TICKET=should-not-remain-visible"]
    return stale


def _assert_economic_state_hidden(app: AutosportApp) -> None:
    assert app.session is None
    assert "9999" not in app.bank.value
    assert "недоступний" in app.bank.value
    assert all("STALE" not in line for line in app.tickets.lines)
    assert app.tickets.lines


def test_terminal_replay_reopen_failure_is_contained_and_requires_recovery() -> None:
    app = _partial_app(ReplayWorkerMessage(result=object()))
    app._open_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ValueError("workspace state failed validation")
    )

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_replay_worker(app)

    assert app.session is None
    assert app._busy_states == [False]
    assert "недоступний" in app.bank.value
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


def test_replay_error_never_reopens_or_publishes_uncertain_economic_state() -> None:
    app = _partial_app(ReplayWorkerMessage(error="RuntimeError: replay failed"))
    stale = _seed_visible_economic_state(app)
    reopen_calls: list[tuple] = []

    def reopen(*args, **kwargs):
        reopen_calls.append((args, kwargs))
        return _Session(Path("economic-workspace"))

    app._open_session = reopen

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_replay_worker(app)

    assert reopen_calls == []
    assert stale.closed
    _assert_economic_state_hidden(app)
    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert "механічно" in app.status.value
    assert app._evaluation == [
        "Evaluation недоступна: replay не досяг terminal settlement/evaluation boundary."
    ]
    showerror.assert_called_once()


def test_missing_terminal_result_never_reopens_or_publishes_uncertain_economic_state() -> None:
    app = _partial_app(SimpleNamespace(result=None, error=None))
    stale = _seed_visible_economic_state(app)
    reopen_calls: list[tuple] = []

    def reopen(*args, **kwargs):
        reopen_calls.append((args, kwargs))
        return _Session(Path("economic-workspace"))

    app._open_session = reopen

    AutosportApp._poll_replay_worker(app)

    assert reopen_calls == []
    assert stale.closed
    _assert_economic_state_hidden(app)
    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert "механічно заблоковано" in app.status.value
    assert app._evaluation == [
        "Evaluation недоступна: worker не повернув terminal SessionResult."
    ]


def test_successful_terminal_result_reopens_and_publishes_session_state() -> None:
    result = object()
    app = _partial_app(ReplayWorkerMessage(result=result))
    reopened = _Session(Path("economic-workspace"))
    app._open_session = lambda *_args, **_kwargs: reopened
    app._refresh_tickets = lambda: app.tickets.insert("end", "FRESH TICKET")

    with (
        patch("autosport.gui.result_summary", return_value="terminal summary"),
        patch("autosport.gui.evaluation_lines", return_value=["terminal evaluation"]),
    ):
        AutosportApp._poll_replay_worker(app)

    assert app.session is reopened
    assert "10000" in app.bank.value
    assert app.tickets.lines == ["FRESH TICKET"]
    assert app._evaluation == ["terminal evaluation"]
    assert app.status.value == "terminal summary"
    assert app._recovery_required_workspaces == set()


def test_reopen_failure_blocks_second_run_until_successful_exact_workspace_recovery() -> None:
    app = _partial_app(ReplayWorkerMessage(result=object()))
    app._open_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ValueError("workspace state failed validation")
    )

    with patch("autosport.gui.messagebox.showerror"):
        AutosportApp._poll_replay_worker(app)

    exact_workspace = Path("economic-workspace")
    assert exact_workspace in app._recovery_required_workspaces

    run_worker = _RunWorker()
    app.replay_worker = run_worker
    app.dataset_path = Path("dataset")
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._bank_text = lambda: "bank"
    app._refresh_tickets = lambda: None
    app.after = lambda *_args: None

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=exact_workspace),
        patch("autosport.gui.messagebox.showwarning") as showwarning,
    ):
        AutosportApp.run_dataset(app)

    assert run_worker.start_calls == 0
    assert exact_workspace in app._recovery_required_workspaces
    assert "Paper replay заблоковано" in app.status.value
    showwarning.assert_called_once()

    clean_report = SimpleNamespace(
        reconciled_keys=(),
        aborted_uncommitted_keys=(),
        unresolved_without_summary=(),
    )
    reopened_session = _Session(exact_workspace)
    app._open_session = lambda *_args, **_kwargs: reopened_session

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=exact_workspace),
        patch("autosport.gui.reconcile_late_crashes", return_value=clean_report),
        patch("autosport.gui.messagebox.showinfo") as showinfo,
    ):
        AutosportApp.repair_workspace(app)

    assert exact_workspace not in app._recovery_required_workspaces
    showinfo.assert_called_once()

    with patch("autosport.gui.workspace_for_strategy", return_value=exact_workspace):
        AutosportApp.run_dataset(app)

    assert reopened_session.closed
    assert run_worker.start_calls == 1


def test_recovery_gate_is_scoped_to_exact_workspace() -> None:
    app = _partial_app(ReplayWorkerMessage(error="unused"))
    run_worker = _RunWorker()
    app.replay_worker = run_worker
    app.dataset_path = Path("dataset")
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._recovery_required_workspaces = {Path("blocked-economic-workspace")}
    app._bank_text = lambda: "bank"
    app._refresh_tickets = lambda: None
    app.after = lambda *_args: None

    with patch(
        "autosport.gui.workspace_for_strategy",
        return_value=Path("healthy-economic-workspace"),
    ):
        AutosportApp.run_dataset(app)

    assert Path("blocked-economic-workspace") in app._recovery_required_workspaces
    assert run_worker.start_calls == 1


def test_failed_recovery_keeps_economic_state_hidden_and_does_not_reopen() -> None:
    app = _partial_app(ReplayWorkerMessage(error="unused"))
    app.replay_worker = _RunWorker()
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    exact_workspace = Path("economic-workspace")
    app._recovery_required_workspaces = {exact_workspace}
    stale = _seed_visible_economic_state(app)
    reopen_calls: list[tuple] = []

    def reopen(*args, **kwargs):
        reopen_calls.append((args, kwargs))
        return _Session(exact_workspace)

    app._open_session = reopen

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=exact_workspace),
        patch(
            "autosport.gui.reconcile_late_crashes",
            side_effect=RuntimeError("recovery validation failed"),
        ),
        patch("autosport.gui.messagebox.showerror") as showerror,
    ):
        AutosportApp.repair_workspace(app)

    assert stale.closed
    assert reopen_calls == []
    _assert_economic_state_hidden(app)
    assert exact_workspace in app._recovery_required_workspaces
    assert "не завершено" in app.status.value
    assert any("recovery validation failed" in line for line in app._logs)
    showerror.assert_called_once()


def test_unresolved_recovery_keeps_economic_state_hidden_and_does_not_reopen() -> None:
    app = _partial_app(ReplayWorkerMessage(error="unused"))
    app.replay_worker = _RunWorker()
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    exact_workspace = Path("economic-workspace")
    app._recovery_required_workspaces = {exact_workspace}
    stale = _seed_visible_economic_state(app)
    reopen_calls: list[tuple] = []

    def reopen(*args, **kwargs):
        reopen_calls.append((args, kwargs))
        return _Session(exact_workspace)

    app._open_session = reopen
    unresolved_report = SimpleNamespace(
        reconciled_keys=(),
        aborted_uncommitted_keys=(),
        unresolved_without_summary=("run-1",),
    )

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=exact_workspace),
        patch("autosport.gui.reconcile_late_crashes", return_value=unresolved_report),
        patch("autosport.gui.messagebox.showwarning") as showwarning,
    ):
        AutosportApp.repair_workspace(app)

    assert stale.closed
    assert reopen_calls == []
    _assert_economic_state_hidden(app)
    assert exact_workspace in app._recovery_required_workspaces
    assert "economic state приховано" in app.status.value
    showwarning.assert_called_once()


def test_resolved_recovery_reopens_before_publishing_and_clears_exact_workspace() -> None:
    app = _partial_app(ReplayWorkerMessage(error="unused"))
    app.replay_worker = _RunWorker()
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    exact_workspace = Path("economic-workspace")
    app._recovery_required_workspaces = {exact_workspace}
    stale = _seed_visible_economic_state(app)
    clean_report = SimpleNamespace(
        reconciled_keys=("run-1",),
        aborted_uncommitted_keys=(),
        unresolved_without_summary=(),
    )
    reopened = _Session(exact_workspace)
    app._open_session = lambda *_args, **_kwargs: reopened

    def refresh_tickets() -> None:
        app.tickets.delete(0, "end")
        app.tickets.insert("end", "FRESH RECOVERED TICKET")

    app._refresh_tickets = refresh_tickets

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=exact_workspace),
        patch("autosport.gui.reconcile_late_crashes", return_value=clean_report),
        patch("autosport.gui.messagebox.showinfo") as showinfo,
    ):
        AutosportApp.repair_workspace(app)

    assert stale.closed
    assert app.session is reopened
    assert "10000" in app.bank.value
    assert app.tickets.lines == ["FRESH RECOVERED TICKET"]
    assert exact_workspace not in app._recovery_required_workspaces
    assert "готовий" in app.status.value
    showinfo.assert_called_once()


def test_post_recovery_reopen_failure_keeps_state_hidden_and_quarantined() -> None:
    app = _partial_app(ReplayWorkerMessage(error="unused"))
    app.replay_worker = _RunWorker()
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    exact_workspace = Path("economic-workspace")
    app._recovery_required_workspaces = {exact_workspace}
    clean_report = SimpleNamespace(
        reconciled_keys=("run-1",),
        aborted_uncommitted_keys=(),
        unresolved_without_summary=(),
    )
    app._open_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ValueError("reopened state invalid")
    )
    app.bank.set("STALE BANKROLL=9999")
    app.tickets.lines = ["STALE TICKET=should-not-remain-visible"]

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=exact_workspace),
        patch("autosport.gui.reconcile_late_crashes", return_value=clean_report),
        patch("autosport.gui.messagebox.showerror") as showerror,
    ):
        AutosportApp.repair_workspace(app)

    _assert_economic_state_hidden(app)
    assert exact_workspace in app._recovery_required_workspaces
    assert "reopened state invalid" in app._logs[-1]
    showerror.assert_called_once()
