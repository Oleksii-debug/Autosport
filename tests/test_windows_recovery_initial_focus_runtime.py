from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.session import AutosportSession
from autosport.windows_gui import WindowsAutosportApp


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="real Tk keyboard-focus acceptance runs on the Windows candidate",
)


def test_recovery_startup_real_tk_focus_resolves_to_repair_action(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    with (
        patch("autosport.gui.default_workspace", return_value=workspace),
        patch(
            "autosport.gui.AutosportSession",
            side_effect=ValueError("corrupt paper state"),
        ),
    ):
        app = WindowsAutosportApp()

    try:
        # Give the real top-level keyboard focus first. The product's deferred
        # recovery callback must then move focus to the existing Repair action
        # when Tk processes its first mapped/idle turn.
        app.focus_force()
        app.update()

        assert app._startup_economic_error == "ValueError: corrupt paper state"
        assert app.focus_get() is app.repair_button
    finally:
        app.destroy()


def test_ready_startup_real_tk_does_not_force_repair_focus(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    session = AutosportSession(workspace, "10000")

    try:
        with (
            patch("autosport.gui.default_workspace", return_value=workspace),
            patch("autosport.gui.AutosportSession", return_value=session),
        ):
            app = WindowsAutosportApp()

        try:
            app.focus_force()
            app.update()

            assert app._startup_economic_error is None
            assert app.focus_get() is not app.repair_button
        finally:
            app.destroy()
    finally:
        session.close()
