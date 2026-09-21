from __future__ import annotations

from pathlib import Path

from . import accessibility_audit as _base_audit
from .windows_accessible_gui import (
    WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID,
    AccessibleWindowsAutosportApp,
)


def run_accessibility_audit(output_path: str | Path) -> int:
    """Run the canonical audit with the packaged Windows status surface included.

    ``accessibility_audit`` remains the single audit implementation. This wrapper
    only supplies the actual packaged GUI class and temporarily extends its
    expected UIA contract with the Windows operational-status Value surface.
    Every mutation is restored before returning so imports elsewhere observe the
    canonical base module unchanged.
    """

    automation_id = WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID
    previous_app_class = _base_audit.WindowsAutosportApp
    had_patterns = automation_id in _base_audit._REQUIRED_PATTERNS
    previous_patterns = _base_audit._REQUIRED_PATTERNS.get(automation_id)
    had_role = automation_id in _base_audit._EXPECTED_ROLES
    previous_role = _base_audit._EXPECTED_ROLES.get(automation_id)

    _base_audit.WindowsAutosportApp = AccessibleWindowsAutosportApp
    _base_audit._REQUIRED_PATTERNS[automation_id] = {"VALUE"}
    _base_audit._EXPECTED_ROLES[automation_id] = "TEXT"
    try:
        return _base_audit.run_accessibility_audit(output_path)
    finally:
        _base_audit.WindowsAutosportApp = previous_app_class
        if had_patterns:
            _base_audit._REQUIRED_PATTERNS[automation_id] = previous_patterns
        else:
            _base_audit._REQUIRED_PATTERNS.pop(automation_id, None)
        if had_role:
            _base_audit._EXPECTED_ROLES[automation_id] = previous_role
        else:
            _base_audit._EXPECTED_ROLES.pop(automation_id, None)
