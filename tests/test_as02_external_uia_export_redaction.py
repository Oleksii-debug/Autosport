"""Plan 6 §2: WebView2 external UIA export privacy source contract.

Portable static evidence only; no Windows-run or physical NVDA assertion.
"""
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"


def _audit() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def test_exported_control_identity_and_mismatches_are_trusted_or_fixed() -> None:
    audit = _audit()
    assert "automation_id = [string]$spec.automation_id" in audit
    assert "automation_id = [string]$element.Current.AutomationId" not in audit
    assert "name = $(if ($currentName -ceq [string]$spec.name)" in audit
    assert "control_type = $(if ($controlType -ceq [string]$spec.expected_control_type)" in audit
    assert "name = $currentName" not in audit
    assert "control_type = $controlType" not in audit
    assert "actual='$currentName'" not in audit
    assert "actual='$controlType'" not in audit


def test_mismatched_controls_still_fail_closed_without_raw_provider_values() -> None:
    audit = _audit()
    assert "if ($currentName -cne [string]$spec.name)" in audit
    assert "$controlType -cne [string]$spec.expected_control_type" in audit
    assert "external UIA Name mismatch" in audit
    assert "external UIA ControlType mismatch" in audit
    assert "duplicate external UIA control identity" in audit
    assert "required_pattern_available = $patternOk" in audit
    assert "human_tested = $false" in audit
    assert "nvda_verified = $false" in audit
    assert "real_money_execution = $false" in audit


def test_untrusted_path_and_status_fields_scrubbed_before_json_and_stdout() -> None:
    audit = _audit()
    start = audit.index("    # Keep UIA validation in memory; scrub provider-controlled names")
    end = audit.index(
        "    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Output -Encoding utf8"
    )
    scrub = audit[start:end]
    for field in (
        "launch_working_directory",
        "runtime_witness_path",
        "emergency_stop_journal_path",
        "root_name",
        "duplicate_launch_dialog_title",
        "emergency_stop_status_text",
        "live_region_event_text",
        "runtime_browser_version",
    ):
        assert "'" + field + "'" in scrub
    assert "$report[$field] = '[redacted]'" in scrub
    assert "$report.live_region_event_automation_id -ceq 'emergency-stop-status'" in scrub
    assert audit.index("} finally {") < start < end
    assert audit.index("if ($report.status -ne 'PASS')") > end
