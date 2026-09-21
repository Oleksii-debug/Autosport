from __future__ import annotations

import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="requires the Windows Tk/UIA runtime")
def test_operational_status_runtime_tracks_canonical_state_and_f5_focus(
    tmp_path,
    monkeypatch,
) -> None:
    from autosport.windows_accessible_gui import (
        OPERATIONAL_STATUS_FOCUS_SHORTCUT,
        AccessibleWindowsAutosportApp,
    )
    from autosport.windows_layout import install_compact_windows_layout

    monkeypatch.setenv("AUTOSPORT_WORKSPACE", str(tmp_path / "workspace"))
    install_compact_windows_layout()
    app = AccessibleWindowsAutosportApp()
    try:
        app.update_idletasks()
        app.update()

        assert app.operational_status.instate(("readonly", "!disabled"))
        assert str(app.operational_status.cget("textvariable")) == str(app.status)

        marker = "Тестовий операційний стан: доступний через UIA Value"
        app.status.set(marker)
        app.update_idletasks()
        assert app.operational_status.get() == marker

        app.strategy.focus_set()
        app.update()
        assert app.focus_get() is app.strategy
        app.event_generate(OPERATIONAL_STATUS_FOCUS_SHORTCUT)
        app.update()
        assert app.focus_get() is app.operational_status
    finally:
        app.destroy()
