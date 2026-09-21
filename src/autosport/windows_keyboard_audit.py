from __future__ import annotations

from pathlib import Path
from typing import Any

from . import keyboard_audit as _base_audit
from .windows_accessible_gui import (
    OPERATIONAL_STATUS_FOCUS_SHORTCUT,
    WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID,
    AccessibleWindowsAutosportApp,
)


def run_keyboard_audit(output_path: str | Path) -> int:
    """Run the keyboard gate against the same GUI class shipped interactively.

    The canonical keyboard-audit implementation remains unchanged. This Windows
    wrapper temporarily adds the operational-status surface to its critical Tab
    set, gates its direct F5 focus shortcut, and swaps in the actual packaged
    accessible GUI class. Every temporary module mutation is restored before
    returning.
    """

    control_name = "operational_status"
    automation_id = WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID
    shortcut = OPERATIONAL_STATUS_FOCUS_SHORTCUT
    previous_app_class = _base_audit.WindowsAutosportApp
    previous_focusable = _base_audit._FOCUSABLE_CONTROLS
    previous_critical_widgets = _base_audit._critical_widgets
    had_automation_id = control_name in _base_audit.AUTOMATION_IDS
    previous_automation_id = _base_audit.AUTOMATION_IDS.get(control_name)
    had_focus_binding = shortcut in _base_audit._FOCUS_BINDINGS
    previous_focus_target = _base_audit._FOCUS_BINDINGS.get(shortcut)

    def critical_widgets(
        app: AccessibleWindowsAutosportApp,
        workbench_dialog: Any | None = None,
    ) -> dict[str, Any]:
        controls = previous_critical_widgets(app, workbench_dialog)
        controls[control_name] = app.operational_status
        return controls

    _base_audit.WindowsAutosportApp = AccessibleWindowsAutosportApp
    _base_audit._FOCUSABLE_CONTROLS = (*previous_focusable, control_name)
    _base_audit._critical_widgets = critical_widgets
    _base_audit.AUTOMATION_IDS[control_name] = automation_id
    _base_audit._FOCUS_BINDINGS[shortcut] = control_name
    try:
        return _base_audit.run_keyboard_audit(output_path)
    finally:
        _base_audit.WindowsAutosportApp = previous_app_class
        _base_audit._FOCUSABLE_CONTROLS = previous_focusable
        _base_audit._critical_widgets = previous_critical_widgets
        if had_automation_id:
            _base_audit.AUTOMATION_IDS[control_name] = previous_automation_id
        else:
            _base_audit.AUTOMATION_IDS.pop(control_name, None)
        if had_focus_binding:
            _base_audit._FOCUS_BINDINGS[shortcut] = previous_focus_target
        else:
            _base_audit._FOCUS_BINDINGS.pop(shortcut, None)
