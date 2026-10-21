"""Falsify contradictory hosted UIA evidence; this is NOT packaged Autosport or NVDA."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.verify_as05_focus_evidence import InvalidEvidence, validate_report


def witness(mode="FocusedElement", status="CONTROL_FOCUSED_UIA_OK", **changes):
    result = dict(
        evidence_class="independent_hosted_windows_uia_control_only",
        probe_mode=mode, status=status,
        process_id_matches=True, control_type_present=True,
        semantic_focus_matches=mode == "FocusedElement" and status == "CONTROL_FOCUSED_UIA_OK",
        host_focus_claimed=True,
        autosport_packaged_uia_qualified=False, autosport_keyboard_qualified=False,
        autosport_emergency_stop_qualified=False, real_money_execution=False,
        human_tested=False, nvda_verified=False, whole_product_complete=False,
    )
    result.update(changes)
    return result


@pytest.mark.parametrize(("mode", "status", "reason"), [
    ("FocusedElement", "CONTROL_FOCUSED_UIA_REJECTED", "FOREIGN_PROCESS"),
    ("FromHandle", "CONTROL_UIA_REJECTED", None),
    ("FocusedElement", "CONTROL_UIA_TIMEOUT", None),
    ("FromHandle", "CONTROL_INFRA_UNAVAILABLE", None),
])
@pytest.mark.parametrize("field", [
    "process_id_matches", "control_type_present", "semantic_focus_matches",
])
def test_negative_cannot_claim_positive_identity(mode, status, reason, field):
    attrs = dict(process_id_matches=False, control_type_present=False,
                 semantic_focus_matches=False)
    if reason:
        attrs["focus_reject_reason"] = reason
    if status == "CONTROL_UIA_TIMEOUT":
        attrs["last_allowlisted_stage"] = "FOCUSED_ELEMENT_PROBE"
    attrs[field] = True
    with pytest.raises(InvalidEvidence):
        validate_report(witness(mode=mode, status=status, **attrs))


def test_fromhandle_cannot_attest_semantic_focus():
    with pytest.raises(InvalidEvidence):
        validate_report(witness(mode="FromHandle", status="CONTROL_UIA_OK",
                                semantic_focus_matches=True))


@pytest.mark.parametrize(("mode", "status", "kwargs"), [
    ("FromHandle", "CONTROL_FOCUS_NOT_OWNED", dict(host_focus_claimed=False)),
    ("FocusedElement", "CONTROL_HOST_UNAVAILABLE", dict(host_focus_claimed=True)),
    ("FocusedElement", "CONTROL_FOCUS_NOT_OWNED", dict(host_focus_claimed=True)),
])
def test_invalid_mode_or_host_foreground(mode, status, kwargs):
    with pytest.raises(InvalidEvidence):
        validate_report(witness(mode=mode, status=status, process_id_matches=False,
                                control_type_present=False, semantic_focus_matches=False,
                                **kwargs))


@pytest.mark.parametrize(("mode", "status", "kwargs"), [
    ("FromHandle", "CONTROL_FOCUS_NOT_OWNED", dict(host_focus_claimed=False)),
    ("FocusedElement", "CONTROL_HOST_UNAVAILABLE", dict(host_focus_claimed=True)),
])
def test_cli_fails_closed_without_secret_echo(tmp_path, mode, status, kwargs):
    path = tmp_path / "invalid-evidence.json"
    record = witness(mode=mode, status=status, process_id_matches=False,
                     control_type_present=False, semantic_focus_matches=False, **kwargs)
    path.write_text(json.dumps(record), encoding="utf-8")
    cli = Path(__file__).resolve().parents[1] / "scripts" / "verify_as05_focus_evidence.py"
    done = subprocess.run([sys.executable, str(cli), str(path)],
                          capture_output=True, text=True, timeout=10, check=False)
    assert done.returncode == 2
    assert done.stdout.strip() == "INVALID_MACHINE_EVIDENCE"
    assert done.stderr == ""


def test_negative_valid_diagnostic_does_not_pass():
    result = validate_report(witness(status="CONTROL_FOCUSED_UIA_REJECTED",
                                     process_id_matches=False, control_type_present=False,
                                     semantic_focus_matches=False,
                                     focus_reject_reason="FOREIGN_PROCESS"))
    assert result["diagnostic_success"] is False
    assert result["nvda_verified"] is False


def test_positive_valid_diagnostic_never_upgrades_product():
    result = validate_report(witness())
    assert result["diagnostic_success"] is True
    assert result["autosport_packaged_uia_qualified"] is False
    assert result["nvda_verified"] is False


def test_windows_ci_runs_new_regressions_not_merely_triggers_them():
    flow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" /
            "windows-uia-environment-control.yml").read_text(encoding="utf-8")
    assert flow.count("tests/test_as05_inconsistent_machine_evidence.py") == 2
    assert "if: ${{ !cancelled() }}" in flow
    assert "github.event.pull_request.head.sha || github.sha" in flow
