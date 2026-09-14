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
    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)

    def delete(self, _start, _end) -> None:
        self.lines.clear()

    def insert(self, _index, value: str) -> None:
        self.lines.append(value)


class _RaisingCloseSession:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.closed = False

    def close(self) -> None:
        self.closed = True
        raise OSError("simulated session close failure")


class _TerminalWorker:
    def __init__(self, message: ReplayWorkerMessage) -> None:
        self._message = message
        self.busy = False

    def poll(self):
        message = self._message
        self._message = None
        return message


def _base_partial_app() -> AutosportApp:
    app = object.__new__(AutosportApp)
    app.workspace = Path("root-workspace")
    app._active_workspace = Path("economic-workspace")
    app._active_strategy_id = "baseline-v1"
    app._active_research_plan = None
    app._recovery_required_workspaces = set()
    app._closing = False
    app.session = _RaisingCloseSession(Path("economic-workspace"))
    app.bank = _Value("STALE BANKROLL=9999")
    app.tickets = _Tickets("STALE TICKET=should-not-remain-visible")
    app.status = _Value()
    app._logs: list[str] = []
    app._append_log = lambda text: app._logs.append(text)
    return app


def test_uncertain_state_is_hidden_before_raising_session_teardown() -> None:
    app = _base_partial_app()
    stale = app.session

    teardown_succeeded = AutosportApp._hide_uncertain_economic_state(
        app,
        "ECONOMIC STATE QUARANTINED",
    )

    assert stale is not None
    assert stale.closed
    assert teardown_succeeded is False
    assert app.session is None
    assert "9999" not in app.bank.value
    assert "недоступний" in app.bank.value
    assert app.tickets.lines == ["ECONOMIC STATE QUARANTINED"]
    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert any(
        "secondary=OSError: simulated session close failure" in line
        for line in app._logs
    )


def test_replay_primary_error_survives_raising_session_teardown() -> None:
    app = _base_partial_app()
    app.replay_worker = _TerminalWorker(
        ReplayWorkerMessage(error="RuntimeError: replay failed")
    )
    app._busy_states: list[bool] = []
    app._evaluation: list[str] = []
    app._set_replay_controls_busy = lambda busy: app._busy_states.append(busy)
    app._set_evaluation_lines = lambda lines: setattr(app, "_evaluation", list(lines))
    app.after = lambda *_args: (_ for _ in ()).throw(
        AssertionError("terminal replay message must not reschedule polling")
    )

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_replay_worker(app)

    assert app.session is None
    assert "9999" not in app.bank.value
    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert any("secondary=OSError: simulated session close failure" in line for line in app._logs)
    assert any("Paper replay помилка: RuntimeError: replay failed" in line for line in app._logs)
    showerror.assert_called_once_with(
        "Автоспорт",
        "Paper replay помилка: RuntimeError: replay failed",
    )


def test_recovery_stops_before_reconcile_after_pre_reconcile_teardown_failure() -> None:
    app = _base_partial_app()
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._open_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("recovery must not reopen after teardown failure")
    )

    exact_workspace = Path("economic-workspace")
    with (
        patch("autosport.gui.workspace_for_strategy", return_value=exact_workspace),
        patch("autosport.gui.reconcile_late_crashes") as reconcile,
        patch("autosport.gui.messagebox.showerror") as showerror,
    ):
        AutosportApp.repair_workspace(app)

    reconcile.assert_not_called()
    assert app.session is None
    assert "9999" not in app.bank.value
    assert "недоступний" in app.bank.value
    assert exact_workspace in app._recovery_required_workspaces
    assert any("secondary=OSError: simulated session close failure" in line for line in app._logs)
    assert any("session teardown" in line for line in app._logs)
    assert "не завершено" in app.status.value
    showerror.assert_called_once()
    assert "session teardown" in showerror.call_args.args[1]
