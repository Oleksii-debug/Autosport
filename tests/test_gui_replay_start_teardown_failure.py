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


class _Listbox:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def delete(self, *_args) -> None:
        self.lines.clear()

    def insert(self, _where, value: str) -> None:
        self.lines.append(value)


class _NeverStartWorker:
    def __init__(self) -> None:
        self.busy = False
        self.start_calls = 0

    def start(self, _task) -> bool:
        self.start_calls += 1
        return True


def test_replay_start_teardown_failure_detaches_and_quarantines_exact_prior_workspace(
    tmp_path: Path,
) -> None:
    prior_workspace = tmp_path / "prior-workspace"
    selected_workspace = tmp_path / "selected-workspace"
    app = object.__new__(AutosportApp)
    app.workspace = tmp_path / "root"
    app.dataset_path = tmp_path / "dataset"
    app._active_workspace = prior_workspace
    app._active_strategy_id = "baseline-v1"
    app._active_research_plan = None
    app._startup_economic_error = None
    app._recovery_required_workspaces = set()
    app.live_worker = SimpleNamespace(busy=False)
    app.replay_worker = _NeverStartWorker()
    app.status = _Value()
    app.bank = _Value("stale bankroll")
    app.tickets = _Listbox()
    app.speed_text = _Value("Подієвий — максимально швидко")
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    logs: list[str] = []
    app._append_log = logs.append

    detached_before_close: list[bool] = []

    class _FailingSession:
        workspace = prior_workspace

        def close(self) -> None:
            detached_before_close.append(app.session is None)
            raise RuntimeError("teardown exploded")

    app.session = _FailingSession()

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=selected_workspace),
        patch("autosport.gui.messagebox.showerror") as showerror,
    ):
        AutosportApp.run_dataset(app)

    assert detached_before_close == [True]
    assert app.session is None
    assert app.replay_worker.start_calls == 0
    assert app._active_workspace == prior_workspace
    assert app._recovery_required_workspaces == {prior_workspace}
    assert selected_workspace not in app._recovery_required_workspaces
    assert "недоступний до підтвердженого завершального стану/відновлення" in app.bank.value
    assert any("стан попереднього економічного сеансу приховано" in line for line in app.tickets.lines)
    assert any("робоча область=" in line and str(prior_workspace) in line for line in logs)
    assert any("цільовий повтор не стартував" in line for line in logs)
    showerror.assert_called_once()
    assert "не вдалося завершити попередній економічний сеанс" in showerror.call_args.args[1]
