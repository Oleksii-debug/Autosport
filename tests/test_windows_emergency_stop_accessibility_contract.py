from pathlib import Path

from autosport.accessibility_audit import _EXPECTED_ROLES, _REQUIRED_PATTERNS
from autosport.gui import AUTOMATION_IDS
from autosport.keyboard_audit import _ACTION_BINDINGS, _FOCUSABLE_CONTROLS
from autosport.windows_gui import (
    WINDOWS_BANKROLL_AUTOMATION_ID,
    WINDOWS_EMERGENCY_STOP_AUTOMATION_ID,
    WINDOWS_EMERGENCY_STOP_HOTKEY,
)
from autosport.windows_layout import WINDOWS_SHELL_AUTOMATION_IDS
from autosport.windows_manual_calculation import WORKBENCH_AUTOMATION_IDS


def test_emergency_stop_has_unique_packaged_uia_identity_and_all_machine_gates() -> None:
    emergency_id = WINDOWS_EMERGENCY_STOP_AUTOMATION_ID
    occupied_base_ids = (
        set(AUTOMATION_IDS.values())
        | set(WINDOWS_SHELL_AUTOMATION_IDS.values())
        | set(WORKBENCH_AUTOMATION_IDS.values())
        | {WINDOWS_BANKROLL_AUTOMATION_ID}
    )

    assert emergency_id == 209
    assert emergency_id not in occupied_base_ids
    assert _REQUIRED_PATTERNS[emergency_id] == {"INVOKE"}
    assert _EXPECTED_ROLES[emergency_id] == "PUSH_BUTTON"
    assert _ACTION_BINDINGS[WINDOWS_EMERGENCY_STOP_HOTKEY] == "emergency_stop"
    assert "emergency_stop" in _FOCUSABLE_CONTROLS

    external_audit = (
        Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"
    ).read_text(encoding="utf-8")
    emergency_spec = (
        "[ordered]@{ key = 'emergency_stop'; automation_id = '209'; "
        "name = 'Аварійний STOP виконання / Emergency execution STOP (Ctrl+Shift+S)'; "
        "required_pattern = 'Invoke'; require_external_focus = $true; "
        "expected_control_type = 'ControlType.Button'; require_named_rows = $false }"
    )
    assert emergency_spec in external_audit
    assert external_audit.count("automation_id = '209'") == 1
