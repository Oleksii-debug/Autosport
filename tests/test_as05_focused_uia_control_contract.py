"""Fail-closed differential hosted Windows UIA diagnostics, not Autosport/NVDA acceptance."""
from pathlib import Path
import json
import subprocess
import sys

import pytest

from scripts.verify_as05_focus_evidence import InvalidEvidence, validate_report

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/windows_uia_environment_control.ps1").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github/workflows/windows-uia-environment-control.yml").read_text(encoding="utf-8")


def report(mode="FocusedElement", status="CONTROL_FOCUSED_UIA_OK", **alterations):
    data = dict(evidence_class="independent_hosted_windows_uia_control_only",
                probe_mode=mode, status=status, process_id_matches=True,
                control_type_present=True, semantic_focus_matches=True,
                host_focus_claimed=True,
                autosport_packaged_uia_qualified=False,
                autosport_keyboard_qualified=False,
                autosport_emergency_stop_qualified=False,
                real_money_execution=False, human_tested=False,
                nvda_verified=False, whole_product_complete=False)
    data.update(alterations)
    return data


def test_real_windows_provider_and_independent_killable_mta_worker():
    for marker in ("[System.Windows.Automation.AutomationElement]::FromHandle(",
                   "[System.Windows.Automation.AutomationElement]::FocusedElement",
                   "Add-Type -AssemblyName UIAutomationClient",
                   "$probeProcess.WaitForExit(10000)", "$probeProcess.Kill($true)",
                   "'-Sta'", "'-Mta'"):
        assert marker in SCRIPT


def test_true_focused_element_requires_native_and_external_semantic_identity():
    for marker in ("$focusInput.Focus()", "$focusInput.Focused",
                   "focus_claimed = $focusClaimed", "Current.ProcessId",
                   "Current.AutomationId", "Current.Name",
                   "Current.ControlType.ProgrammaticName", "ControlType.Edit",
                   "autosport-uia-control-focused-input",
                   "Autosport UIA hosted focus anchor",
                   "$result.semantic_focus_matches = $true",
                   "$probe.semantic_focus_matches -eq $true"):
        assert marker in SCRIPT


def test_native_focus_claim_requires_real_foreground_window_witness():
    # WinForms.Focus()/Focused alone are not an OS-wide keyboard/UIA oracle.
    assert '[DllImport("user32.dll")]' in SCRIPT
    assert "public static extern IntPtr GetForegroundWindow();" in SCRIPT
    assert "$foregroundOwned = [AutosportUiaNativeForeground]::GetForegroundWindow() -eq $form.Handle" in SCRIPT
    assert "$focusClaimed = [bool]$focusInput.Focused -and [bool]$foregroundOwned" in SCRIPT
    assert "if ($ProbeMode -eq 'FocusedElement' -and $native.focus_claimed -ne $true)" in SCRIPT
    assert "$result.status = 'CONTROL_FOCUS_NOT_OWNED'" in SCRIPT


def test_focus_rejection_is_classified_without_exposing_foreign_desktop_metadata():
    for marker in ("'FOREIGN_PROCESS'", "'AUTOMATION_ID_MISMATCH'",
                   "'ACCESSIBLE_NAME_MISMATCH'", "'ROLE_MISMATCH'",
                   "$result.focus_reject_reason = $focusRejectReason",
                   "$allowedFocusRejectReasons -ccontains [string]$probe.focus_reject_reason"):
        assert marker in SCRIPT
    bad = report(status="CONTROL_FOCUSED_UIA_REJECTED",
                 focus_reject_reason="RAW_FOREIGN_WINDOW_TITLE")
    with pytest.raises(InvalidEvidence):
        validate_report(bad)
    good = report(status="CONTROL_FOCUSED_UIA_REJECTED",
                  focus_reject_reason="FOREIGN_PROCESS")
    assert validate_report(good)["diagnostic_success"] is False
    with pytest.raises(InvalidEvidence):
        validate_report(report(status="CONTROL_FOCUSED_UIA_REJECTED"))
    with pytest.raises(InvalidEvidence):
        validate_report(report(focus_reject_reason="FOREIGN_PROCESS"))


def test_fromhandle_remains_default_but_success_is_per_probe():
    assert "[string]$ProbeMode = 'FromHandle'" in SCRIPT
    assert "$expectedPass = if ($ProbeMode -eq 'FocusedElement')" in SCRIPT
    assert "if ($result.status -cne $expectedPass) { exit 1 }" in SCRIPT
    assert "'CONTROL_FOCUSED_UIA_REJECTED'" in SCRIPT
    assert "'CONTROL_UIA_REJECTED'" in SCRIPT


def test_timeout_classification_does_not_implicitly_pass():
    for marker in ("'FOCUSED_ELEMENT_PROBE'", "'FOCUSED_ELEMENT_RETURNED'",
                   "'CONTROL_UIA_TIMEOUT'", "'CONTROL_FOCUS_NOT_OWNED'",
                   "'CONTROL_PROBE_UNTRUSTED'", "'INVALID'"):
        assert marker in SCRIPT


def test_workflow_checks_exact_head_and_both_real_probes():
    assert "github.event.pull_request.head.sha || github.sha" in WORKFLOW
    assert "Compare real FromHandle and FocusedElement" in WORKFLOW
    assert "$fromHandleExit = $LASTEXITCODE" in WORKFLOW
    assert "$focusedElementExit = $LASTEXITCODE" in WORKFLOW
    assert "if ($fromHandleExit -ne 0 -or $focusedElementExit -ne 0) { exit 1 }" in WORKFLOW
    assert "if: always()" in WORKFLOW
    assert "uia-control.json" in WORKFLOW
    assert "uia-focus-control.json" in WORKFLOW
    assert "verify_as05_focus_evidence.py" in WORKFLOW
    # The evidence schema verifier must still run after a rejected focus probe.
    validator = "      - name: Validate the two fixed-class diagnostic reports (never product UIA)\n"
    assert validator + "        if: ${{ !cancelled() }}\n" in WORKFLOW
    assert "if ($fromHandleEvidenceExit -ne 0 -or $focusEvidenceExit -ne 0) { exit 1 }" in WORKFLOW


def test_machine_report_good_focus_is_only_diagnostic():
    verdict = validate_report(report())
    assert verdict["diagnostic_success"] is True
    assert verdict["autosport_packaged_uia_qualified"] is False
    assert verdict["human_tested"] is False
    assert verdict["nvda_verified"] is False


@pytest.mark.parametrize("field", ["process_id_matches", "control_type_present",
                                  "semantic_focus_matches", "host_focus_claimed"])
def test_invalid_or_missing_required_identity_cannot_pass(field):
    with pytest.raises(InvalidEvidence):
        validate_report(report(**{field: False}))
    damaged = report()
    del damaged[field]
    with pytest.raises(InvalidEvidence):
        validate_report(damaged)


@pytest.mark.parametrize("alterations", [
    {"probe_mode": "FutureUnsupported"},
    {"status": "CONTROL_UIA_OK"},
    {"human_tested": True},
    {"nvda_verified": True},
    {"real_money_execution": True},
    {"whole_product_complete": True},
    {"private_raw_exception": "some secret"},
])
def test_truth_and_privacy_flags_fail_closed(alterations):
    with pytest.raises(InvalidEvidence):
        validate_report(report(**alterations))


def test_focus_timeout_reports_allowlisted_stage_but_is_never_pass():
    evidence = report(status="CONTROL_UIA_TIMEOUT", last_allowlisted_stage="FOCUSED_ELEMENT_PROBE")
    assert validate_report(evidence)["diagnostic_success"] is False
    evidence["last_allowlisted_stage"] = "PRIVATE_CREDENTIAL"
    with pytest.raises(InvalidEvidence):
        validate_report(evidence)


def test_fixed_focus_stage_cannot_be_inserted_for_positive_result():
    with pytest.raises(InvalidEvidence):
        validate_report(report(last_allowlisted_stage="FOCUSED_ELEMENT_RETURNED"))


def test_fromhandle_positive_is_not_packaged_product_pass():
    verdict = validate_report(report(mode="FromHandle", status="CONTROL_UIA_OK", semantic_focus_matches=False))
    assert verdict["diagnostic_success"] is True
    assert verdict["autosport_packaged_uia_qualified"] is False


@pytest.mark.parametrize(("field", "status"), [
    ("probe_mode", "CONTROL_FOCUSED_UIA_OK"),
    ("status", "CONTROL_FOCUSED_UIA_OK"),
    ("focus_reject_reason", "CONTROL_FOCUSED_UIA_REJECTED"),
    ("last_allowlisted_stage", "CONTROL_UIA_TIMEOUT"),
])
@pytest.mark.parametrize("malformed", [
    ["PRIVATE_CANARY_DO_NOT_ECHO"],
    {"secret": "PRIVATE_CANARY_DO_NOT_ECHO"},
], ids=("json-array", "json-object"))
def test_malformed_diagnostic_enum_rejected_as_fixed_invalid_evidence(field, status, malformed):
    # JSON arrays/objects are unhashable; no TypeError/traceback can escape
    # from any fixed-code enum field, even for rejected or timeout reports.
    bad = report(status=status)
    bad[field] = malformed
    with pytest.raises(InvalidEvidence):
        validate_report(bad)


# The independent Windows control is machine evidence. Duplicate JSON keys must
# never be silently overwritten into a synthetic positive status or an attestation.
@pytest.mark.parametrize(("field", "contradiction"), [
    ("status", "CONTROL_UIA_REJECTED"),
    ("probe_mode", "UnsupportedMode"),
    ("nvda_verified", True),
    ("human_tested", True),
    ("real_money_execution", True),
    ("whole_product_complete", True),
    ("autosport_packaged_uia_qualified", True),
    ("process_id_matches", False),
    ("control_type_present", False),
    ("evidence_class", "human_nvda_verified"),
    ("status", "CONTROL_UIA_OK"),
])
def test_duplicate_json_evidence_fails_closed_without_private_output(tmp_path, field, contradiction):
    record = report(mode="FromHandle", status="CONTROL_UIA_OK",
                    semantic_focus_matches=False)
    good = json.dumps(record, ensure_ascii=False)
    raw = "{" + json.dumps(field) + ":" + json.dumps(contradiction) + "," + good[1:]
    path = tmp_path / "uia-report.json"
    path.write_text(raw, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_as05_focus_evidence.py"), str(path)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 2
    assert result.stdout.strip() == "INVALID_MACHINE_EVIDENCE"
    assert result.stderr == ""


def test_duplicate_json_nested_keys_also_fail_closed_without_echo(tmp_path):
    record = report(mode="FromHandle", status="CONTROL_UIA_OK",
                    semantic_focus_matches=False)
    path = tmp_path / "uia-report.json"
    path.write_text(
        json.dumps(record)[:-1] + ',"extraneous":{"secret":"CANARY_PRIVATE","secret":"another"}}',
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_as05_focus_evidence.py"), str(path)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 2
    assert result.stdout.strip() == "INVALID_MACHINE_EVIDENCE"
    assert result.stderr == ""
    assert "CANARY_PRIVATE" not in result.stdout


def test_unambiguous_json_still_produces_diagnostic_only(tmp_path):
    record = report(mode="FromHandle", status="CONTROL_UIA_OK",
                    semantic_focus_matches=False)
    path = tmp_path / "uia-report.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_as05_focus_evidence.py"), str(path)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["diagnostic_success"] is True
    assert verdict["autosport_packaged_uia_qualified"] is False
    assert verdict["human_tested"] is False
    assert verdict["nvda_verified"] is False

@pytest.mark.parametrize("reason", [
    "FOREGROUND_LOST_BEFORE_UIA",
    "FOREGROUND_LOST_DURING_UIA",
])
def test_transient_native_foreground_loss_is_a_negative_diagnostic(reason):
    rejected = report(status="CONTROL_FOCUSED_UIA_REJECTED",
                      process_id_matches=False, control_type_present=False,
                      semantic_focus_matches=False, focus_reject_reason=reason)
    verdict = validate_report(rejected)
    assert verdict["diagnostic_success"] is False
    assert verdict["autosport_packaged_uia_qualified"] is False


def test_disposable_mta_probe_samples_os_foreground_on_both_sides_of_uia_call():
    probe = SCRIPT.split("if ($Mode -eq 'probe') {", 1)[1].split(
        "$result = New-Evidence -Code 'CONTROL_INFRA_UNAVAILABLE'", 1
    )[0]
    before = "$foregroundBefore = [AutosportUiaProbeForeground]::GetForegroundWindow()"
    query = "$root = [System.Windows.Automation.AutomationElement]::FocusedElement"
    after = "$foregroundAfter = [AutosportUiaProbeForeground]::GetForegroundWindow()"
    assert probe.index(before) < probe.index(query) < probe.index(after)
    assert "$foregroundBefore -ne [IntPtr]::new($WindowHandle)" in probe
    assert "$foregroundAfter -ne [IntPtr]::new($WindowHandle)" in probe
    assert "FOREGROUND_LOST_BEFORE_UIA" in probe
    assert "FOREGROUND_LOST_DURING_UIA" in probe
    assert "$focusRejectReason = 'FOREGROUND_LOST_BEFORE_UIA'" in probe
    assert "$focusRejectReason = 'FOREGROUND_LOST_DURING_UIA'" in probe
    assert "Add-Type -TypeDefinition @'" in probe
