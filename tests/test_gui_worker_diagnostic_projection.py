from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import autosport.gui as gui
from autosport.gui import AutosportApp


_RAW_A = "Authorization: Bearer AUTOSPORT-RAW-DIAGNOSTIC-A"
_RAW_B = "password=AUTOSPORT-RAW-DIAGNOSTIC-B"


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
        self.error = error

    def poll(self):
        return SimpleNamespace(error=self.error, result=None)


def _render_dataset_error(raw: str, monkeypatch) -> str:
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox,
        "showerror",
        lambda title, message: dialogs.append((title, message)),
    )
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
    return "\n".join(app._logs + [app.status.value] + [item for pair in dialogs for item in pair])


def _render_live_error(raw: str) -> str:
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


def _render_replay_error(raw: str, monkeypatch) -> str:
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox,
        "showerror",
        lambda title, message: dialogs.append((title, message)),
    )
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
    return "\n".join(app._logs + [app.status.value] + [item for pair in dialogs for item in pair])


def test_worker_error_presentation_is_independent_of_raw_diagnostic(monkeypatch) -> None:
    renderers = (
        lambda raw: _render_dataset_error(raw, monkeypatch),
        _render_live_error,
        lambda raw: _render_replay_error(raw, monkeypatch),
    )

    for render in renderers:
        first = render(_RAW_A)
        second = render(_RAW_B)

        assert first == second
        for forbidden in (
            "AUTOSPORT-RAW-DIAGNOSTIC",
            "Authorization",
            "Bearer",
            "password",
        ):
            assert forbidden.casefold() not in first.casefold()

    assert "DATASET_VALIDATION_FAILURE" in renderers[0](_RAW_A)
    assert "LIVE_OBSERVATION_FAILURE" in renderers[1](_RAW_A)
    assert "REPLAY_WORKER_FAILURE" in renderers[2](_RAW_A)
