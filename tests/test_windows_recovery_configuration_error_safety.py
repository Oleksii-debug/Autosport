from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from autosport.localization import text
from autosport.windows_gui import WindowsAutosportApp


class _BrokenTextError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification failed")


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


def test_recovery_configuration_error_with_broken_str_stays_fail_closed() -> None:
    app = object.__new__(WindowsAutosportApp)
    app._closing = False
    app._dataset_busy = False
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app.recovery_worker = SimpleNamespace(busy=False)
    app.status = _Value()

    def fail_configuration():
        raise _BrokenTextError()

    app._selected_replay_configuration = fail_configuration

    with patch("autosport.windows_gui.messagebox.showerror") as showerror:
        WindowsAutosportApp.repair_workspace(app)

    expected = text(
        "ui.error.recovery.configuration",
        detail="_BrokenTextError: exception details unavailable",
    )
    assert app.status.value == text("ui.status.recovery.configuration_rejected")
    showerror.assert_called_once_with(text("ui.dialog.title"), expected)
