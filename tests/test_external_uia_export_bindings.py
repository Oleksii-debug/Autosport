"""Privacy projection and workflow admission units, not real product UIA proof."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "external_uia_audit.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-build.yml"
FLAGS = (
    "launch_working_directory_verified", "duplicate_dialog_title_verified",
    "emergency_stop_status_text_present", "live_region_event_text_present",
    "runtime_browser_version_present", "runtime_witness_matches_workspace",
    "emergency_stop_journal_matches_workspace",
)


def _projection() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("    # Project equality/presence from original in-memory observations")
    end = source.index("    # Keep UIA validation in memory; scrub provider-controlled names", start)
    return source[start:end]


def _admission() -> str:
    source = WORKFLOW.read_text(encoding="utf-8")
    start = source.index("          if ($null -eq $external -or $external.status -ne 'PASS')")
    end = source.index("          $verificationPath =", start)
    return source[start:end]


def _pwsh(code: str, payload: dict, workspace: Path) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env=dict(os.environ, UIA_UNIT_JSON=json.dumps(payload), UIA_UNIT_WORKSPACE=str(workspace)),
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_workflow_requires_actual_boolean_proofs_before_accepting_scrubbed_report():
    admission = _admission()
    for field in FLAGS:
        assert f"$external.{field} -isnot [bool]" in admission
        assert f"$external.{field} -ne $true" in admission
    assert "(Join-Path $expectedWorkspace 'execution-stop.jsonl') -PathType Leaf" in admission
    assert "(Join-Path $expectedWorkspace 'webview2-runtime-witness.json') -PathType Leaf" in admission
    assert "[string]$external.duplicate_launch_dialog_title -ne" not in admission


def test_failure_diagnostics_export_only_numeric_location_and_fixed_state():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "$report.automation_error_line = [int]$_.InvocationInfo.ScriptLineNumber" in source
    assert "$report.automation_error_category = [int]$_.CategoryInfo.Category" in source
    assert "$report.duplicate_process_state = 'RUNNING'" in source
    assert "$report.duplicate_process_state = 'EXITED'" in source
    assert "$report.duplicate_process_family_count = [int]$duplicateFamilyIds.Count" in source
    assert "$report.duplicate_visible_window_count = [int]$duplicateNativeHandles.Count" in source


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="Real PowerShell required")
@pytest.mark.parametrize("wrong_field", [None, "launch_working_directory", "duplicate_launch_dialog_title",
    "emergency_stop_status_text", "live_region_event_text", "runtime_browser_version",
    "runtime_witness_path", "emergency_stop_journal_path"])
def test_real_projection_uses_original_values_not_redacted_sentinels(tmp_path, wrong_field):
    payload = {
        "launch_working_directory": str(tmp_path),
        "duplicate_launch_dialog_title": "Автоспорт — помилка запуску",
        "emergency_stop_status_text": "STOP confirmed",
        "live_region_event_text": "STOP confirmed",
        "runtime_browser_version": "actual-version-fixture",
        "runtime_witness_path": str(tmp_path / "webview2-runtime-witness.json"),
        "emergency_stop_journal_path": str(tmp_path / "execution-stop.jsonl"),
    }
    if wrong_field:
        payload[wrong_field] = "[redacted]" if wrong_field.endswith(("path", "title", "directory")) else ""
    code = (
        "$ErrorActionPreference='Stop'\n$report=[ordered]@{}\n"
        "$data=ConvertFrom-Json $env:UIA_UNIT_JSON\n"
        "foreach($p in $data.PSObject.Properties){$report[$p.Name]=$p.Value}\n"
        "$Workspace=$env:UIA_UNIT_WORKSPACE\n$launchWorkingDirectory=$Workspace\n"
        + _projection()
        + "\n$out=[ordered]@{}\n"
        + "foreach($name in @('" + "','".join(FLAGS) + "')){$out[$name]=$report[$name]}\n"
        + "$out|ConvertTo-Json -Compress\n"
    )
    result = _pwsh(code, payload, tmp_path)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert all(type(v) is bool for v in observed.values())
    assert sum(not v for v in observed.values()) == (1 if wrong_field else 0)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="Real PowerShell required")
@pytest.mark.parametrize(("field", "bad_value"), [(None, True)] + [
    (field, value) for field in FLAGS for value in (False, "true", None, 1, "MISSING")
])
def test_real_workflow_gate_requires_true_booleans(tmp_path, field, bad_value):
    for name in ("execution-stop.jsonl", "webview2-runtime-witness.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    payload = {name: True for name in FLAGS}
    payload.update(status="PASS", duplicate_launch_status="PASS", duplicate_launch_exit_code=2,
        normal_close_status="PASS", normal_close_exit_code=0, emergency_stop_activation_status="PASS",
        live_region_event_status="PASS", live_region_event_count=1,
        live_region_event_automation_id="emergency-stop-status", keyboard_shortcuts_status="PASS",
        f2_focus_automation_id="301", f8_focus_automation_id="204", runtime_witness_status="PASS",
        real_money_execution=False, human_tested=False, nvda_verified=False)
    if field is not None:
        if bad_value == "MISSING":
            del payload[field]
        else:
            payload[field] = bad_value
    code = ("$ErrorActionPreference='Stop'\n$external=ConvertFrom-Json $env:UIA_UNIT_JSON\n"
        "$expectedWorkspace=$env:UIA_UNIT_WORKSPACE\n" + _admission() + "\n'ACCEPTED'\n")
    result = _pwsh(code, payload, tmp_path)
    if field is None:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ACCEPTED"
    else:
        assert result.returncode != 0
        assert "ACCEPTED" not in result.stdout
