from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_EXTERNAL_UIA_AUDIT = _ROOT / "scripts" / "external_uia_audit.ps1"
_WINDOWS_GUI = _ROOT / "src" / "autosport" / "windows_gui.py"


def test_external_uia_audit_covers_packaged_bankroll_summary() -> None:
    audit = _EXTERNAL_UIA_AUDIT.read_text(encoding="utf-8")
    windows_gui = _WINDOWS_GUI.read_text(encoding="utf-8")

    assert "WINDOWS_BANKROLL_AUTOMATION_ID = 205" in windows_gui
    assert 'tk_uia.set_acc_name(self.bank_summary, "Віртуальний банк")' in windows_gui

    expected = (
        "[ordered]@{ key = 'bankroll'; automation_id = '205'; "
        "name = 'Віртуальний банк'; required_pattern = 'Value'; "
        "require_external_focus = $true; expected_control_type = $null; "
        "require_named_rows = $false }"
    )
    assert expected in audit
    assert audit.count("automation_id = '205'") == 1
