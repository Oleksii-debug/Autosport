from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.gui import AutosportApp


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _HeadlessAutosportApp(AutosportApp):
    """Exercise product startup without requiring a display server."""

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


class _Button:
    def __init__(self) -> None:
        self.states: list[tuple[str, ...]] = []

    def state(self, values) -> None:
        self.states.append(tuple(values))


class _ImmediateWorker:
    def __init__(self) -> None:
        self.busy = False
        self.result = None
        self.start_calls = 0

    def start(self, task) -> bool:
        self.start_calls += 1
        self.result = task()
        return True


class _NeverStartWorker:
    def __init__(self) -> None:
        self.busy = False
        self.start_calls = 0

    def start(self, _task) -> bool:
        self.start_calls += 1
        return True


def _string_var(*_args, value: str = "", **_kwargs) -> _Value:
    return _Value(value)


def test_corrupt_economic_startup_keeps_shell_and_live_observation_reachable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    with (
        patch("autosport.gui.tk.Tk.__init__", return_value=None),
        patch("autosport.gui.tk.StringVar", side_effect=_string_var),
        patch("autosport.gui.default_workspace", return_value=workspace),
        patch("autosport.gui.AutosportSession", side_effect=ValueError("corrupt paper state")),
    ):
        app = _HeadlessAutosportApp()

    assert app.shell_built
    assert app.accessibility_configured
    assert app.close_protocol_bound
    assert app.session is None
    assert app._startup_economic_error == "ValueError: corrupt paper state"
    assert workspace in app._recovery_required_workspaces
    assert "Read-only live snapshot доступний" in app.status.value
    assert "недоступний до успішного recovery" in app.bank.value

    live_result = object()
    provider = object()
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = _ImmediateWorker()
    app.live_mode_text = _Value("Public preview — без ключа")
    app.live_status = _Value()
    app.live_refresh_button = _Button()
    scheduled: list[tuple[int, object]] = []
    app.after = lambda delay, callback: scheduled.append((delay, callback))

    with (
        patch("autosport.gui.ParlayApiTableTennisProvider", return_value=provider) as provider_factory,
        patch("autosport.gui.observe_workspace_once", return_value=live_result) as observe,
    ):
        AutosportApp.refresh_live_snapshot(app)

    assert app.live_worker.start_calls == 1
    assert app.live_worker.result is live_result
    provider_factory.assert_called_once_with(None, public_preview=True)
    observe.assert_called_once_with(workspace, provider, max_items=250)
    assert app.live_refresh_button.states == [("disabled",)]
    assert "read-only live observation" in app.status.value
    assert scheduled and scheduled[0][0] == 100

    replay_worker = _NeverStartWorker()
    app.replay_worker = replay_worker
    app.live_worker = SimpleNamespace(busy=False)
    app.dataset_path = tmp_path / "dataset"
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._append_log = lambda _text: None

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=workspace),
        patch("autosport.gui.messagebox.showwarning") as showwarning,
    ):
        AutosportApp.run_dataset(app)

    assert replay_worker.start_calls == 0
    assert "непідтверджений terminal state" in app.status.value
    showwarning.assert_called_once()


def test_valid_economic_startup_preserves_existing_ready_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    session = SimpleNamespace(
        workspace=workspace,
        strategy_id="baseline-v1",
        book=SimpleNamespace(balance=10000, committed_stake=0),
    )

    with (
        patch("autosport.gui.tk.Tk.__init__", return_value=None),
        patch("autosport.gui.tk.StringVar", side_effect=_string_var),
        patch("autosport.gui.default_workspace", return_value=workspace),
        patch("autosport.gui.AutosportSession", return_value=session),
    ):
        app = _HeadlessAutosportApp()

    assert app.session is session
    assert app._startup_economic_error is None
    assert app._recovery_required_workspaces == set()
    assert app.status.value.startswith("Готово.")
    assert "10000" in app.bank.value
