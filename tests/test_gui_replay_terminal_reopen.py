from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from autosport.gui import AutosportApp
from autosport.replay_worker import ReplayWorkerMessage


class _Value:
    def __init__(self) -> None:
        self.value = ""

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
    def __init__(self, message: ReplayWorkerMessage) -> None:
        self._message = message

    def poll(self):
        message = self._message
        self._message = None
        return message


def _partial_app(message: ReplayWorkerMessage) -> AutosportApp:
    app = object.__new__(AutosportApp)
    app.replay_worker = _TerminalWorker(message)
    app._active_strategy_id = "baseline-v1"
    app._active_research_plan = None
    app._active_workspace = SimpleNamespace(__str__=lambda self: "workspace")
    app.session = None
    app.status = _Value()
    app.bank = _Value()
    app.tickets = _Tickets()
    app._busy_states = []
    app._logs = []
    app._evaluation = []
    app._set_replay_controls_busy = lambda busy: app._busy_states.append(busy)
    app._append_log = lambda text: app._logs.append(text)
    app._set_evaluation_lines = lambda lines: setattr(app, "_evaluation", list(lines))
    app.after = lambda *_args: (_ for _ in ()).throw(AssertionError("terminal message must not reschedule polling"))
    return app


def test_terminal_replay_reopen_failure_is_contained_and_user_visible() -> None:
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
    assert "terminal state не можна безпечно підтвердити" in app.status.value
    assert any("workspace state failed validation" in line for line in app._logs)
    showerror.assert_called_once()


def test_replay_error_context_survives_secondary_reopen_failure() -> None:
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
    showerror.assert_called_once()
