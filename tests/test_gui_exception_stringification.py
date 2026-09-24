from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.gui import AutosportApp
from autosport.localization import text


class _BrokenTextError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification failed")


class _BrokenTypeNameMeta(type):
    def __getattribute__(cls, name: str):
        if name == "__name__":
            raise RuntimeError("exception type-name lookup failed")
        return super().__getattribute__(name)


class _BrokenMetadataAndTextError(RuntimeError, metaclass=_BrokenTypeNameMeta):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification failed")


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _Listbox:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def delete(self, *_args) -> None:
        self.lines.clear()

    def insert(self, _where, value: str) -> None:
        self.lines.append(value)


class _HeadlessAutosportApp(AutosportApp):
    def title(self, _value: str) -> None:
        return None

    def geometry(self, _value: str) -> None:
        return None

    def minsize(self, _width: int, _height: int) -> None:
        return None

    def _build(self) -> None:
        self.shell_built = True

    def update_idletasks(self) -> None:
        return None

    def _configure_accessibility(self) -> None:
        self.accessibility_configured = True

    def protocol(self, _name: str, _callback) -> None:
        self.close_protocol_bound = True


def _string_var(*_args, value: str = "", **_kwargs) -> _Value:
    return _Value(value)


def _recovery_app(workspace: Path) -> tuple[AutosportApp, list[str]]:
    app = object.__new__(AutosportApp)
    app._closing = False
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app.workspace = workspace
    app.status = _Value()
    app._recovery_required_workspaces = set()
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._hide_uncertain_economic_state = lambda _message: True
    logs: list[str] = []
    app._append_log = logs.append
    return app, logs


def test_startup_exception_with_broken_str_keeps_shell_reachable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    with (
        patch("autosport.gui.tk.Tk.__init__", return_value=None),
        patch("autosport.gui.tk.StringVar", side_effect=_string_var),
        patch("autosport.gui.default_workspace", return_value=workspace),
        patch("autosport.gui.AutosportSession", side_effect=_BrokenTextError()),
    ):
        app = _HeadlessAutosportApp()

    assert app.shell_built
    assert app.accessibility_configured
    assert app.close_protocol_bound
    assert app.session is None
    assert app._startup_economic_error == "RuntimeError: <повідомлення недоступне>"
    assert app._recovery_required_workspaces == {workspace}
    assert app.status.value == text("ui.status.startup.recovery_required")
    assert "недоступний до успішного відновлення" in app.bank.value


def test_startup_exception_with_hostile_type_metadata_and_str_keeps_shell_reachable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"

    with (
        patch("autosport.gui.tk.Tk.__init__", return_value=None),
        patch("autosport.gui.tk.StringVar", side_effect=_string_var),
        patch("autosport.gui.default_workspace", return_value=workspace),
        patch("autosport.gui.AutosportSession", side_effect=_BrokenMetadataAndTextError()),
    ):
        app = _HeadlessAutosportApp()

    assert app.shell_built
    assert app.accessibility_configured
    assert app.close_protocol_bound
    assert app.session is None
    assert app._startup_economic_error == "RuntimeError: <повідомлення недоступне>"
    assert app._recovery_required_workspaces == {workspace}
    assert app.status.value == text("ui.status.startup.recovery_required")


def test_teardown_exception_with_broken_str_still_quarantines_and_returns_false(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = object.__new__(AutosportApp)
    app._active_workspace = workspace
    app._recovery_required_workspaces = set()
    app.bank = _Value("stale bankroll")
    app.tickets = _Listbox()
    logs: list[str] = []
    app._append_log = logs.append

    class _FailingSession:
        def __init__(self, session_workspace: Path) -> None:
            self.workspace = session_workspace

        def close(self) -> None:
            raise _BrokenTextError()

    app.session = _FailingSession(workspace)

    result = AutosportApp._hide_uncertain_economic_state(
        app,
        "Economic state hidden pending terminal transition.",
    )

    assert result is False
    assert app.session is None
    assert app._recovery_required_workspaces == {workspace}
    assert app.tickets.lines == ["Economic state hidden pending terminal transition."]
    assert len(logs) == 1
    assert f"робоча область={workspace}" in logs[0]
    assert "вторинна_помилка=RuntimeError: <повідомлення недоступне>" in logs[0]


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt, SystemExit])
def test_teardown_process_control_propagates_after_exact_workspace_quarantine(
    tmp_path: Path,
    control_exception: type[BaseException],
) -> None:
    workspace = tmp_path / "workspace"
    app = object.__new__(AutosportApp)
    app._active_workspace = workspace
    app._recovery_required_workspaces = set()
    app.bank = _Value("stale bankroll")
    app.tickets = _Listbox()
    logs: list[str] = []
    app._append_log = logs.append

    class _InterruptedSession:
        def __init__(self, session_workspace: Path) -> None:
            self.workspace = session_workspace

        def close(self) -> None:
            raise control_exception()

    app.session = _InterruptedSession(workspace)

    with pytest.raises(control_exception):
        AutosportApp._hide_uncertain_economic_state(
            app,
            "Economic state hidden pending terminal transition.",
        )

    assert app.session is None
    assert app._recovery_required_workspaces == {workspace}
    assert app.tickets.lines == ["Economic state hidden pending terminal transition."]
    assert logs == []


def test_reconcile_failure_with_broken_str_keeps_recovery_actionable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    app, logs = _recovery_app(workspace)

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=workspace),
        patch("autosport.gui.reconcile_late_crashes", side_effect=_BrokenTextError()),
        patch("autosport.gui.messagebox.showerror") as showerror,
    ):
        AutosportApp.repair_workspace(app)

    expected = "Відновлення робочої області відхилено закрито при помилці: RuntimeError: <повідомлення недоступне>"
    assert app._recovery_required_workspaces == {workspace}
    assert expected in logs
    assert "економічний стан лишається недоступним" in app.status.value
    showerror.assert_called_once_with("Автоспорт", expected)


def test_post_recovery_reopen_failure_with_broken_str_keeps_recovery_actionable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app, logs = _recovery_app(workspace)

    def _open_session(_strategy_id, _research_plan):
        raise _BrokenTextError()

    app._open_session = _open_session
    report = SimpleNamespace(
        reconciled_keys=(),
        aborted_uncommitted_keys=(),
        unresolved_without_summary=(),
    )

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=workspace),
        patch("autosport.gui.reconcile_late_crashes", return_value=report),
        patch("autosport.gui.messagebox.showerror") as showerror,
    ):
        AutosportApp.repair_workspace(app)

    expected = "Повторне відкриття робочої області після відновлення відхилено закрито при помилці: RuntimeError: <повідомлення недоступне>"
    assert app._recovery_required_workspaces == {workspace}
    assert expected in logs
    assert "стан економічного сеансу лишається недоступним" in app.status.value
    showerror.assert_called_once_with("Автоспорт", expected)


def test_post_replay_reopen_failure_with_hostile_exception_keeps_feedback_actionable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = object.__new__(AutosportApp)
    app._active_workspace = workspace
    app._active_strategy_id = "baseline-v1"
    app._active_research_plan = None
    app._recovery_required_workspaces = set()
    app.session = None
    app.bank = _Value("stale bankroll")
    app.tickets = _Listbox()
    app.status = _Value()
    logs: list[str] = []
    evaluation: list[str] = []
    app._append_log = logs.append
    app._set_evaluation_lines = evaluation.extend
    app._set_replay_controls_busy = lambda _busy: None
    app.replay_worker = SimpleNamespace(
        poll=lambda: SimpleNamespace(error=None, result=object())
    )

    def _open_session(_strategy_id, _research_plan):
        raise _BrokenMetadataAndTextError()

    app._open_session = _open_session

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_replay_worker(app)

    expected = (
        "Повторне відкриття робочої області після повтору відхилено закрито при помилці: "
        "RuntimeError: <повідомлення недоступне>"
    )
    assert app.session is None
    assert app._recovery_required_workspaces == {workspace}
    assert "недоступний до підтвердженого завершального стану/відновлення" in app.bank.value
    assert app.tickets.lines == [
        "Повтор завершено, але стан економічного сеансу недоступний; виконайте відновлення робочої області."
    ]
    assert evaluation == [
        "Оцінювання недоступне: повторне відкриття робочої області після повтору не пройшло закриту при помилці перевірку."
    ]
    assert logs == [expected]
    assert "економічну робочу область заблоковано" in app.status.value
    showerror.assert_called_once_with("Автоспорт", expected)
