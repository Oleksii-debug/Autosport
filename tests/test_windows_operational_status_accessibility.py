from __future__ import annotations

import inspect
from pathlib import Path

import autosport.accessibility_audit as base_audit
import autosport.keyboard_audit as base_keyboard_audit
import autosport.windows_accessibility_audit as packaged_audit
import autosport.windows_keyboard_audit as packaged_keyboard_audit
from autosport.windows_accessible_gui import (
    OPERATIONAL_STATUS_ACCESSIBLE_DESCRIPTION_UK,
    OPERATIONAL_STATUS_ACCESSIBLE_NAME_UK,
    WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID,
    AccessibleWindowsAutosportApp,
)


def test_operational_status_uses_stable_readonly_uia_value_surface() -> None:
    assert WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID == 206
    assert OPERATIONAL_STATUS_ACCESSIBLE_NAME_UK == "Операційний стан Автоспорт"
    assert "Лише для читання" in OPERATIONAL_STATUS_ACCESSIBLE_DESCRIPTION_UK

    build_source = inspect.getsource(AccessibleWindowsAutosportApp._build)
    assert "textvariable=self.status" in build_source
    assert 'state="readonly"' in build_source
    assert "takefocus=True" in build_source

    accessibility_source = inspect.getsource(
        AccessibleWindowsAutosportApp._configure_accessibility
    )
    assert "WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID" in accessibility_source
    assert "set_acc_name" in accessibility_source
    assert "set_acc_description" in accessibility_source


def test_packaged_accessibility_audit_temporarily_gates_status_value_pattern(
    monkeypatch,
    tmp_path: Path,
) -> None:
    automation_id = WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID
    previous_app = base_audit.WindowsAutosportApp
    previous_patterns = base_audit._REQUIRED_PATTERNS.get(automation_id)
    previous_role = base_audit._EXPECTED_ROLES.get(automation_id)
    had_patterns = automation_id in base_audit._REQUIRED_PATTERNS
    had_role = automation_id in base_audit._EXPECTED_ROLES
    observed: dict[str, object] = {}

    def fake_run(output_path: str | Path) -> int:
        observed["output_path"] = Path(output_path)
        observed["app"] = base_audit.WindowsAutosportApp
        observed["patterns"] = set(base_audit._REQUIRED_PATTERNS[automation_id])
        observed["role"] = base_audit._EXPECTED_ROLES[automation_id]
        return 0

    monkeypatch.setattr(base_audit, "run_accessibility_audit", fake_run)
    destination = tmp_path / "accessibility.json"
    assert packaged_audit.run_accessibility_audit(destination) == 0

    assert observed == {
        "output_path": destination,
        "app": AccessibleWindowsAutosportApp,
        "patterns": {"VALUE"},
        "role": "TEXT",
    }
    assert base_audit.WindowsAutosportApp is previous_app
    assert (automation_id in base_audit._REQUIRED_PATTERNS) is had_patterns
    assert (automation_id in base_audit._EXPECTED_ROLES) is had_role
    if had_patterns:
        assert base_audit._REQUIRED_PATTERNS[automation_id] == previous_patterns
    if had_role:
        assert base_audit._EXPECTED_ROLES[automation_id] == previous_role


def test_packaged_keyboard_audit_uses_real_accessible_gui_and_tabs_to_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    control_name = "operational_status"
    previous_app = base_keyboard_audit.WindowsAutosportApp
    previous_focusable = base_keyboard_audit._FOCUSABLE_CONTROLS
    previous_critical_widgets = base_keyboard_audit._critical_widgets
    had_automation_id = control_name in base_keyboard_audit.AUTOMATION_IDS
    previous_automation_id = base_keyboard_audit.AUTOMATION_IDS.get(control_name)
    observed: dict[str, object] = {}

    def fake_run(output_path: str | Path) -> int:
        observed["output_path"] = Path(output_path)
        observed["app"] = base_keyboard_audit.WindowsAutosportApp
        observed["focusable"] = control_name in base_keyboard_audit._FOCUSABLE_CONTROLS
        observed["automation_id"] = base_keyboard_audit.AUTOMATION_IDS[control_name]
        observed["critical_wrapped"] = (
            base_keyboard_audit._critical_widgets is not previous_critical_widgets
        )
        return 0

    monkeypatch.setattr(base_keyboard_audit, "run_keyboard_audit", fake_run)
    destination = tmp_path / "keyboard.json"
    assert packaged_keyboard_audit.run_keyboard_audit(destination) == 0

    assert observed == {
        "output_path": destination,
        "app": AccessibleWindowsAutosportApp,
        "focusable": True,
        "automation_id": WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID,
        "critical_wrapped": True,
    }
    assert base_keyboard_audit.WindowsAutosportApp is previous_app
    assert base_keyboard_audit._FOCUSABLE_CONTROLS == previous_focusable
    assert base_keyboard_audit._critical_widgets is previous_critical_widgets
    assert (control_name in base_keyboard_audit.AUTOMATION_IDS) is had_automation_id
    if had_automation_id:
        assert base_keyboard_audit.AUTOMATION_IDS[control_name] == previous_automation_id


def test_packaged_entrypoint_selects_accessible_windows_gui() -> None:
    from autosport import windows_entry

    source = inspect.getsource(windows_entry._run_interactive_gui)
    assert "windows_accessible_gui" in source
    machine_source = inspect.getsource(windows_entry.main)
    assert "windows_accessibility_audit" in machine_source
    assert "windows_keyboard_audit" in machine_source
