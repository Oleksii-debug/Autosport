from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import autosport.gui as gui
from autosport.gui import AutosportApp


_SECRET_DETAIL = "RuntimeError: token=AUTOSPORT-GUI-SECRET-SENTINEL; market=winner"
_SAFE_DETAIL = "ValueError: market dataset hash mismatch"


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value


class _Control:
    def __init__(self) -> None:
        self.states: list[tuple[str, ...]] = []

    def state(self, values) -> None:
        self.states.append(tuple(values))


class _TerminalWorker:
    def __init__(self, error: str) -> None:
        self._error = error

    def poll(self):
        return SimpleNamespace(error=self._error, result=None)


def _dialog_capture(monkeypatch) -> list[tuple[str, str]]:
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox,
        "showerror",
        lambda title, message: dialogs.append((title, message)),
    )
    return dialogs


def _dataset_rendered(raw: str, monkeypatch) -> str:
    dialogs = _dialog_capture(monkeypatch)
    app = object.__new__(AutosportApp)
    app._closing = False
    app.dataset_worker = _TerminalWorker(raw)
    app._pending_dataset_path = Path("candidate")
    app.dataset_path = Path("previous")
    app.dataset_text = _Value("previous")
    app.status = _Value()
    app._logs: list[str] = []
    app._append_log = app._logs.append
    app._set_replay_controls_busy = lambda _busy: None

    AutosportApp._poll_dataset_worker(app)

    assert dialogs
    return "\n".join(
        app._logs
        + [app.status.value]
        + [value for pair in dialogs for value in pair]
    )


def _live_rendered(raw: str) -> str:
    app = object.__new__(AutosportApp)
    app._closing = False
    app.live_worker = _TerminalWorker(raw)
    app.live_refresh_button = _Control()
    app.live_status = _Value()
    app.status = _Value()
    app._logs: list[str] = []
    app._append_log = app._logs.append

    AutosportApp._poll_live_worker(app)

    return "\n".join(app._logs + [app.status.value, app.live_status.value])


def _replay_rendered(raw: str, monkeypatch) -> str:
    dialogs = _dialog_capture(monkeypatch)
    app = object.__new__(AutosportApp)
    app.replay_worker = _TerminalWorker(raw)
    app._active_workspace = Path("workspace")
    app._recovery_required_workspaces = set()
    app.status = _Value()
    app._logs: list[str] = []
    app._append_log = app._logs.append
    app._set_replay_controls_busy = lambda _busy: None
    app._hide_uncertain_economic_state = lambda _ticket: None
    app._set_evaluation_lines = lambda _lines: None

    AutosportApp._poll_replay_worker(app)

    assert dialogs
    return "\n".join(
        app._logs
        + [app.status.value]
        + [value for pair in dialogs for value in pair]
    )


def _evidence_rendered(raw: str, monkeypatch) -> str:
    dialogs = _dialog_capture(monkeypatch)
    app = object.__new__(AutosportApp)
    app._closing = False
    app.evidence_export_worker = _TerminalWorker(raw)
    app.status = _Value()
    app._logs: list[str] = []
    app._append_log = app._logs.append
    app._set_replay_controls_busy = lambda _busy: None

    AutosportApp._poll_evidence_export_worker(app)

    assert dialogs
    return "\n".join(
        app._logs
        + [app.status.value]
        + [value for pair in dialogs for value in pair]
    )


def test_base_gui_worker_sinks_use_shared_secret_redaction(monkeypatch) -> None:
    renderers = (
        lambda raw: _dataset_rendered(raw, monkeypatch),
        _live_rendered,
        lambda raw: _replay_rendered(raw, monkeypatch),
        lambda raw: _evidence_rendered(raw, monkeypatch),
    )

    for render in renderers:
        rendered = render(_SECRET_DETAIL)

        assert "AUTOSPORT-GUI-SECRET-SENTINEL" not in rendered
        assert "[REDACTED]" in rendered
        assert "market=winner" in rendered
        assert "RuntimeError" in rendered


def test_base_gui_redaction_preserves_useful_non_secret_diagnostic(monkeypatch) -> None:
    rendered = _dataset_rendered(_SAFE_DETAIL, monkeypatch)

    assert "ValueError: market dataset hash mismatch" in rendered
    assert "[REDACTED]" not in rendered
