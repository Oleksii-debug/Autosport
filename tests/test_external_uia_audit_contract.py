from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_EXTERNAL_UIA_AUDIT = _ROOT / "scripts" / "external_uia_audit.ps1"
_WINDOWS_GUI = _ROOT / "src" / "autosport" / "windows_gui.py"


def test_external_uia_audit_covers_packaged_readonly_surfaces() -> None:
    audit = _EXTERNAL_UIA_AUDIT.read_text(encoding="utf-8")
    windows_gui = _WINDOWS_GUI.read_text(encoding="utf-8")

    assert "WINDOWS_BANKROLL_AUTOMATION_ID = 205" in windows_gui
    assert 'text("ui.accessibility.bankroll.name")' in windows_gui
    assert 'text("ui.accessibility.bankroll.description")' in windows_gui

    expected_bankroll = (
        "[ordered]@{ key = 'bankroll'; automation_id = '205'; "
        "name = 'Віртуальний банк'; required_pattern = 'Value'; "
        "require_external_focus = $true; expected_control_type = 'ControlType.Edit'; "
        "require_named_rows = $false; require_value_read_only = $true }"
    )
    expected_shell_state = (
        "[ordered]@{ key = 'shell_state'; automation_id = '302'; "
        "name = 'Стан вибраної поверхні'; required_pattern = 'Value'; "
        "require_external_focus = $true; expected_control_type = 'ControlType.Edit'; "
        "require_named_rows = $false; require_value_read_only = $true }"
    )
    expected_owner_state = (
        "[ordered]@{ key = 'owner_economic_status'; automation_id = '306'; "
        "name = 'Стан економічних меж власника'; required_pattern = 'Value'; "
        "require_external_focus = $true; expected_control_type = 'ControlType.Edit'; "
        "require_named_rows = $false; require_value_read_only = $true }"
    )
    assert expected_bankroll in audit
    assert expected_shell_state in audit
    assert expected_owner_state in audit
    assert audit.count("automation_id = '205'") == 1
    assert audit.count("automation_id = '302'") == 1
    assert audit.count("automation_id = '306'") == 1
    assert audit.count("require_value_read_only = $true") == 3


def test_external_uia_audit_requires_semantic_control_type_for_every_critical_control() -> None:
    audit = _EXTERNAL_UIA_AUDIT.read_text(encoding="utf-8")
    expected_types = {
        "101": "ControlType.Button",
        "102": "ControlType.Button",
        "103": "ControlType.ComboBox",
        "104": "ControlType.ComboBox",
        "105": "ControlType.Button",
        "106": "ControlType.ComboBox",
        "107": "ControlType.Button",
        "108": "ControlType.Button",
        "201": "ControlType.List",
        "202": "ControlType.Edit",
        "203": "ControlType.List",
        "204": "ControlType.List",
        "205": "ControlType.Edit",
        "301": "ControlType.ComboBox",
        "302": "ControlType.Edit",
        "303": "ControlType.Button",
        "304": "ControlType.List",
        "305": "ControlType.Button",
        "306": "ControlType.Edit",
        "307": "ControlType.List",
    }

    for automation_id, control_type in expected_types.items():
        matching_lines = [
            line
            for line in audit.splitlines()
            if f"automation_id = '{automation_id}'" in line
        ]
        assert len(matching_lines) == 1
        assert f"expected_control_type = '{control_type}'" in matching_lines[0]

    assert "external UIA ControlType mismatch" in audit


def test_external_uia_audit_gates_shell_names_patterns_and_rows() -> None:
    audit = _EXTERNAL_UIA_AUDIT.read_text(encoding="utf-8")
    assert "key = 'shell_navigation'; automation_id = '301'; name = 'Навігація екранами Автоспорт'" in audit
    assert "key = 'shell_open'; automation_id = '303'; name = 'Перейти до робочої поверхні'" in audit
    assert "key = 'shell_details'; automation_id = '304'; name = 'Контракт вибраного екрана'" in audit
    assert "key = 'owner_economic_open'; automation_id = '305'; name = 'Економічні межі власника'" in audit
    assert "key = 'owner_economic_readback'; automation_id = '307'; name = 'Точні економічні межі власника'" in audit
    assert "automation_id=$($spec.automation_id): no externally exposed named ListItem rows" in audit
    assert "automation_id=$($spec.automation_id): missing external UIA $($spec.required_pattern) pattern" in audit


def test_external_uia_audit_fails_closed_on_writable_readonly_value() -> None:
    audit = _EXTERNAL_UIA_AUDIT.read_text(encoding="utf-8")

    assert "[System.Windows.Automation.ValuePattern]::Pattern" in audit
    assert "$valuePattern.Current.IsReadOnly" in audit
    assert "value_read_only_required = $valueReadOnlyRequired" in audit
    assert "value_read_only = $valueReadOnly" in audit
    assert "$valueReadOnlyRequired -and $valueReadOnly -ne $true" in audit
    assert "external UIA ValuePattern is writable or read-only state unavailable" in audit
