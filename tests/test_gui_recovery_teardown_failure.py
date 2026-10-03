from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.gui import AutosportApp
from autosport.localization import text
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
    app._close_teardown_unresolved = False
    app._close_teardown_workspace = None
    app._last_teardown_failure_workspace = None
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
        "вторинна_помилка=OSError: simulated session close failure" in line
        for line in app._logs
    )


def test_teardown_failure_does_not_trust_hostile_session_workspace_metadata() -> None:
    app = _base_partial_app()
    fallback_workspace = Path("fallback-economic-workspace")
    app._active_workspace = fallback_workspace

    class _HostileWorkspaceSession:
        @property
        def workspace(self):
            raise AssertionError("session workspace property must not run after teardown failure")

        def close(self) -> None:
            raise OSError("primary close failure")

    app.session = _HostileWorkspaceSession()

    teardown_succeeded = AutosportApp._hide_uncertain_economic_state(
        app,
        "ECONOMIC STATE QUARANTINED",
    )

    assert teardown_succeeded is False
    assert app.session is None
    assert fallback_workspace in app._recovery_required_workspaces
    assert app.tickets.lines == ["ECONOMIC STATE QUARANTINED"]
    assert any(
        "вторинна_помилка=OSError: primary close failure" in line
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
    assert any("вторинна_помилка=OSError: simulated session close failure" in line for line in app._logs)
    assert any("Помилка паперового повтору: RuntimeError: replay failed" in line for line in app._logs)
    showerror.assert_called_once_with(
        "Автоспорт",
        "Помилка паперового повтору: RuntimeError: replay failed",
    )


def _configure_close_app(app: AutosportApp) -> tuple[list[str], list[str]]:
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app.evidence_export_worker = SimpleNamespace(busy=False)
    app.live_status = _Value()
    destroy_calls: list[str] = []
    bell_calls: list[str] = []
    app.destroy = lambda: destroy_calls.append("destroy")
    app.bell = lambda: bell_calls.append("bell")
    return destroy_calls, bell_calls


def test_close_teardown_failure_keeps_window_open_and_quarantines_workspace() -> None:
    app = _base_partial_app()
    stale = app.session
    destroy_calls, bell_calls = _configure_close_app(app)

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp.close_app(app)
        AutosportApp.close_app(app)

    assert stale is not None
    assert stale.closed
    assert app.session is None
    assert app._closing is False
    assert app._close_teardown_unresolved is True
    assert app._close_teardown_workspace == Path("economic-workspace")
    assert destroy_calls == []
    assert bell_calls == ["bell", "bell"]
    assert Path("economic-workspace") in app._recovery_required_workspaces
    assert "9999" not in app.bank.value
    assert app.tickets.lines == [text("ui.status.close.teardown_ticket")]
    assert app.status.value == text("ui.status.close.teardown_blocked")
    assert app._logs[-1] == text("ui.status.close.teardown_blocked")
    assert any(
        "вторинна_помилка=OSError: simulated session close failure" in line
        for line in app._logs
    )
    assert showerror.call_count == 2
    showerror.assert_called_with(
        text("ui.dialog.title"),
        text("ui.error.close.teardown"),
    )


def test_close_teardown_latch_clears_only_after_matching_recovery() -> None:
    app = _base_partial_app()
    failed_workspace = Path("economic-workspace")
    other_workspace = Path("other-workspace")
    app._close_teardown_unresolved = True
    app._close_teardown_workspace = failed_workspace
    app._recovery_required_workspaces = {failed_workspace, other_workspace}

    app._recovery_required_workspaces.discard(other_workspace)
    AutosportApp._clear_close_teardown_after_recovery(app, other_workspace)
    assert app._close_teardown_unresolved is True
    assert app._close_teardown_workspace == failed_workspace

    app._recovery_required_workspaces.discard(failed_workspace)
    AutosportApp._clear_close_teardown_after_recovery(app, failed_workspace)
    assert app._close_teardown_unresolved is False
    assert app._close_teardown_workspace is None


def test_close_process_control_failure_never_destroys_window() -> None:
    app = _base_partial_app()
    exact_workspace = Path("interrupt-workspace")

    class _InterruptedSession:
        def __init__(self) -> None:
            self.workspace = exact_workspace

        def close(self) -> None:
            raise KeyboardInterrupt("close interrupted")

    app.session = _InterruptedSession()
    destroy_calls, bell_calls = _configure_close_app(app)

    try:
        AutosportApp.close_app(app)
    except KeyboardInterrupt as exc:
        assert str(exc) == "close interrupted"
    else:
        raise AssertionError("KeyboardInterrupt must propagate after fail-closed quarantine")

    assert app.session is None
    assert app._closing is False
    assert app._close_teardown_unresolved is True
    assert app._close_teardown_workspace == exact_workspace
    assert destroy_calls == []
    assert bell_calls == []
    assert exact_workspace in app._recovery_required_workspaces
    assert "9999" not in app.bank.value
    assert app.tickets.lines == [text("ui.status.close.teardown_ticket")]


def test_close_success_destroys_only_after_session_teardown() -> None:
    app = _base_partial_app()
    close_order: list[str] = []

    class _SuccessfulSession:
        workspace = Path("economic-workspace")

        def close(self) -> None:
            close_order.append("session.close")

    app.session = _SuccessfulSession()
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app.evidence_export_worker = SimpleNamespace(busy=False)
    app.live_status = _Value()
    app.bell = lambda: (_ for _ in ()).throw(
        AssertionError("successful close must not bell")
    )
    app.destroy = lambda: close_order.append("destroy")

    AutosportApp.close_app(app)

    assert close_order == ["session.close", "destroy"]
    assert app.session is None
    assert app._closing is True
    assert app._close_teardown_unresolved is False
    assert app._close_teardown_workspace is None
    assert app._recovery_required_workspaces == set()


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
    assert any("вторинна_помилка=OSError: simulated session close failure" in line for line in app._logs)
    assert any("попередній економічний сеанс" in line for line in app._logs)
    assert "не запущено" in app.status.value
    showerror.assert_called_once()
    assert "попередній економічний сеанс" in showerror.call_args.args[1]
