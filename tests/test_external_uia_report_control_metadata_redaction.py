"""Plan 6 §4Б: exported UIA metadata must not echo arbitrary provider values.

These portable source-contract regressions complement the Windows-only packaged
external UI Automation test. They do not claim physical NVDA verification.
"""
from pathlib import Path

_AUDIT = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"


def test_exported_automation_id_is_trusted_spec_identity_not_provider_metadata():
    script = _AUDIT.read_text(encoding="utf-8")
    assert "automation_id = [string]$spec.automation_id" in script
    assert "automation_id = [string]$element.Current.AutomationId" not in script
    assert "automation_id=$($spec.automation_id): duplicate external UIA control identity" in script


def test_control_type_mismatches_report_fixed_markers_not_provider_values():
    script = _AUDIT.read_text(encoding="utf-8")
    assert "control_type = $(if ($controlType -eq [string]$spec.expected_control_type) { [string]$spec.expected_control_type } else { '[redacted mismatch]' })" in script
    assert 'external UIA ControlType mismatch"' in script
    assert "actual='$controlType'" not in script
    assert "control_type = $controlType" not in script


def test_control_name_and_failure_exports_are_still_redacted():
    script = _AUDIT.read_text(encoding="utf-8")
    assert "'[redacted mismatch]'" in script
    assert "name = $(if ($currentName -eq [string]$spec.name)" in script
    assert "external UIA Name mismatch" in script
    assert "$report | ConvertTo-Json -Depth 8 | Set-Content" in script
    assert "human_tested = $false" in script
    assert "nvda_verified = $false" in script
