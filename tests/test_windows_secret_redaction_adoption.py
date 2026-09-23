from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import autosport.windows_gui as windows_gui
from autosport.windows_gui import WindowsAutosportApp


_SECRET_DETAIL = "RuntimeError: token=AUTOSPORT-WINDOWS-SECRET-SENTINEL; market=winner"
_SAFE_DETAIL = "RuntimeError: recovery validation failed"


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value


class _TerminalWorker:
    def __init__(self, error: str) -> None:
        self._error = error

    def poll(self):
        return SimpleNamespace(error=self._error, result=None)


def _capture_dialogs(monkeypatch) -> list[tuple[str, str]]:
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        windows_gui.messagebox,
        "showerror",
        lambda title, message: dialogs.append((title, message)),
    )
    return dialogs


def _recovery_rendered(raw: str, monkeypatch) -> str:
    dialogs = _capture_dialogs(monkeypatch)
    app = object.__new__(WindowsAutosportApp)
    app.recovery_worker = _TerminalWorker(raw)
    app._recovery_view = object()
    app.bank = _Value()
    app.status = _Value()
    app._logs: list[str] = []
    app._append_log = app._logs.append
    app._set_replay_controls_busy = lambda _busy: None
    app._bank_text = lambda: "safe-bank-summary"
    app._refresh_tickets = lambda: None
    app.after = lambda *_args: None

    WindowsAutosportApp._poll_recovery_worker(app)

    assert dialogs
    return "\n".join(
        app._logs
        + [app.status.value]
        + [value for pair in dialogs for value in pair]
    )


def _replay_rendered(raw: str, monkeypatch) -> str:
    dialogs = _capture_dialogs(monkeypatch)
    app = object.__new__(WindowsAutosportApp)
    app.replay_worker = _TerminalWorker(raw)
    app._active_workspace = Path("workspace")
    app._recovery_view = object()
    app.session = object()
    app.bank = _Value()
    app.status = _Value()
    app._logs: list[str] = []
    app._append_log = app._logs.append
    app._set_replay_controls_busy = lambda _busy: None
    app._block_workspace_for_recovery = lambda _workspace: None
    app._bank_text = lambda: "safe-bank-summary"
    app._refresh_tickets = lambda: None
    app._set_evaluation_lines = lambda _lines: None
    app.after = lambda *_args: None

    WindowsAutosportApp._poll_replay_worker(app)

    assert dialogs
    return "\n".join(
        app._logs
        + [app.status.value]
        + [value for pair in dialogs for value in pair]
    )


def test_windows_worker_sinks_use_shared_secret_redaction(monkeypatch) -> None:
    for render in (
        lambda raw: _recovery_rendered(raw, monkeypatch),
        lambda raw: _replay_rendered(raw, monkeypatch),
    ):
        rendered = render(_SECRET_DETAIL)

        assert "AUTOSPORT-WINDOWS-SECRET-SENTINEL" not in rendered
        assert "[REDACTED]" in rendered
        assert "market=winner" in rendered
        assert "RuntimeError" in rendered


def test_windows_redaction_preserves_useful_non_secret_diagnostic(monkeypatch) -> None:
    rendered = _recovery_rendered(_SAFE_DETAIL, monkeypatch)

    assert "RuntimeError: recovery validation failed" in rendered
    assert "[REDACTED]" not in rendered
