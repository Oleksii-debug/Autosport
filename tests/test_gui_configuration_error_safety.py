from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.gui import AutosportApp
from autosport.localization import text


class _BrokenTextError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification failed")


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


def _configuration_failure_app() -> AutosportApp:
    app = object.__new__(AutosportApp)
    app._closing = False
    app.dataset_worker = SimpleNamespace(busy=False)
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app.status = _Value()

    def fail_configuration():
        raise _BrokenTextError()

    app._selected_replay_configuration = fail_configuration
    return app


def _expected_error(key: str) -> str:
    detail = text(
        "ui.error.exception.message_unavailable",
        exception_type="_BrokenTextError",
    )
    return text(key, detail=detail)


def test_base_recovery_configuration_broken_str_stays_fail_closed() -> None:
    app = _configuration_failure_app()

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp.repair_workspace(app)

    assert app.status.value == text("ui.status.recovery.configuration_rejected")
    showerror.assert_called_once_with(
        text("ui.dialog.title"),
        _expected_error("ui.error.recovery.configuration"),
        parent=app,
    )


def test_base_replay_configuration_broken_str_stays_fail_closed() -> None:
    app = _configuration_failure_app()
    app.dataset_path = Path("dataset")

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp.run_dataset(app)

    assert app.status.value == text("ui.status.replay.configuration_rejected")
    showerror.assert_called_once_with(
        text("ui.dialog.title"),
        _expected_error("ui.error.replay.configuration"),
        parent=app,
    )
