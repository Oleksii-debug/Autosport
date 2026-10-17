"""Fail-closed differential hosted Windows UIA diagnostics, not Autosport/NVDA acceptance."""
from pathlib import Path

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
