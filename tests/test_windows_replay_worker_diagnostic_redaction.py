from __future__ import annotations

from pathlib import Path

from autosport.replay_worker import ReplayWorkerMessage
from autosport.windows_gui import WindowsAutosportApp
import autosport.windows_gui as windows_gui


_SECRET = "Authorization: Bearer AUTOSPORT-REPLAY-SECRET-SENTINEL"


class _ValueSink:
    def __init__(self) -> None:
        self.values: list[str] = []

    def set(self, value: str) -> None:
        self.values.append(value)


class _TerminalReplayWorker:
    def __init__(self, error: str) -> None:
        self._message = ReplayWorkerMessage(error=error)

    def poll(self) -> ReplayWorkerMessage:
        return self._message


class _ReplayPollHarness:
    def __init__(self, error: str) -> None:
        self.replay_worker = _TerminalReplayWorker(error)
        self._active_workspace = Path("economic-workspace")
        self._recovery_view = object()
        self.session = object()
        self.bank = _ValueSink()
        self.status = _ValueSink()
        self.log: list[str] = []
        self.evaluation: list[str] = []
        self.blocked: list[Path] = []
        self.busy_transitions: list[bool] = []
        self.ticket_refreshes = 0

    def _set_replay_controls_busy(self, busy: bool) -> None:
        self.busy_transitions.append(busy)

    def _block_workspace_for_recovery(self, workspace: Path) -> None:
        self.blocked.append(Path(workspace))

    def _bank_text(self) -> str:
        return "safe-bank-summary"

    def _refresh_tickets(self) -> None:
        self.ticket_refreshes += 1

    def _set_evaluation_lines(self, lines: list[str]) -> None:
        self.evaluation = list(lines)

    def _append_log(self, value: str) -> None:
        self.log.append(value)


def _render(app: _ReplayPollHarness, dialogs: list[tuple[str, str]]) -> str:
    return "\n".join(
        app.log
        + app.status.values
        + app.evaluation
        + [title for title, _message in dialogs]
        + [message for _title, message in dialogs]
    )


def test_replay_worker_raw_secret_diagnostic_never_reaches_operator_surfaces(
    monkeypatch,
) -> None:
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        windows_gui.messagebox,
        "showerror",
        lambda title, message: dialogs.append((title, message)),
    )
    app = _ReplayPollHarness(_SECRET)

    WindowsAutosportApp._poll_replay_worker(app)

    assert app.busy_transitions == [False]
    assert app.blocked == [Path("economic-workspace")]
    assert app._recovery_view is None
    assert app.session is None
    assert app.bank.values == ["safe-bank-summary"]
    assert app.ticket_refreshes == 1
    assert app.evaluation
    assert dialogs

    rendered = _render(app, dialogs)
    for forbidden in (
        "AUTOSPORT-REPLAY-SECRET-SENTINEL",
        "Authorization",
        "Bearer",
    ):
        assert forbidden not in rendered

    assert "Помилка паперового повтору" in rendered
    assert "REPLAY_WORKER_FAILURE" in rendered

    dialogs.clear()
    other = _ReplayPollHarness("password=DIFFERENT-RAW-REPLAY-DIAGNOSTIC")
    WindowsAutosportApp._poll_replay_worker(other)
    rendered_other = _render(other, dialogs)

    assert rendered_other == rendered
    assert "DIFFERENT-RAW-REPLAY-DIAGNOSTIC" not in rendered_other
    assert "password" not in rendered_other.casefold()
