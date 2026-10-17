"""Validate the Windows UIA *diagnostic* report without upgrading product claims.

The hosted WinForms control tests Windows UI Automation plumbing only. Even
CONTROL_FOCUSED_UIA_OK is not an Autosport packaged UIA, keyboard, STOP,
Windows/NVDA owner or final-release PASS.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FALSE_FLAGS = (
    "autosport_packaged_uia_qualified", "autosport_keyboard_qualified",
    "autosport_emergency_stop_qualified", "real_money_execution", "human_tested",
    "nvda_verified", "whole_product_complete",
)
PROBES = {
    "FromHandle": {"CONTROL_UIA_OK", "CONTROL_UIA_REJECTED"},
    "FocusedElement": {"CONTROL_FOCUSED_UIA_OK", "CONTROL_FOCUSED_UIA_REJECTED"},
}
INFRA = {
    "CONTROL_INFRA_UNAVAILABLE", "CONTROL_HOST_UNAVAILABLE",
    "CONTROL_FOCUS_NOT_OWNED", "CONTROL_PROBE_FAILED",
    "CONTROL_UIA_TIMEOUT", "CONTROL_PROBE_UNTRUSTED",
}
SUCCESS = {"CONTROL_UIA_OK", "CONTROL_FOCUSED_UIA_OK"}
STAGES = {"FOCUSED_ELEMENT_PROBE", "FOCUSED_ELEMENT_RETURNED", "NO_STAGE", "INVALID"}
FOCUS_REJECT_REASONS = {
    "PROVIDER_OR_PROPERTY_ERROR", "NO_ELEMENT", "FOREIGN_PROCESS",
    "MISSING_TYPE", "AUTOMATION_ID_MISMATCH",
    "ACCESSIBLE_NAME_MISMATCH", "ROLE_MISMATCH",
    "FOREGROUND_LOST_BEFORE_UIA", "FOREGROUND_LOST_DURING_UIA",
}


class InvalidEvidence(ValueError):
    pass



def _reject_duplicate_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Fail closed on ambiguous JSON evidence, including nested duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidEvidence("duplicate JSON field")
        result[key] = value
    return result



def validate_report(report: dict) -> dict:
    if not isinstance(report, dict):
        raise InvalidEvidence("report must be a JSON object")
    if report.get("evidence_class") != "independent_hosted_windows_uia_control_only":
        raise InvalidEvidence("wrong evidence class")
    if any(report.get(key) is not False for key in FALSE_FLAGS):
        raise InvalidEvidence("product or human attestation flag not explicitly false")
    mode = report.get("probe_mode")
    if not isinstance(mode, str) or mode not in PROBES:
        raise InvalidEvidence("unknown probe mode")
    status = report.get("status")
    if not isinstance(status, str) or status not in PROBES[mode] | INFRA:
        raise InvalidEvidence("mode-incompatible or unknown status")
    for key in ("process_id_matches", "control_type_present", "semantic_focus_matches", "host_focus_claimed"):
        if report.get(key) not in (True, False) or type(report[key]) is not bool:
            raise InvalidEvidence(f"missing/invalid fixed boolean: {key}")
    if status in SUCCESS:
        if report["process_id_matches"] is not True or report["control_type_present"] is not True:
            raise InvalidEvidence("positive status missing genuine PID/type witnesses")
        if mode == "FocusedElement" and (report["semantic_focus_matches"] is not True or report["host_focus_claimed"] is not True):
            raise InvalidEvidence("positive focus status lacks native+UIA focus witnesses")
    if status == "CONTROL_FOCUSED_UIA_REJECTED":
        reason = report.get("focus_reject_reason")
        if not isinstance(reason, str) or reason not in FOCUS_REJECT_REASONS:
            raise InvalidEvidence("missing/unknown fixed focus rejection classification")
    elif "focus_reject_reason" in report:
        raise InvalidEvidence("focus rejection classification outside focused rejection")
    if status == "CONTROL_UIA_TIMEOUT" and mode == "FocusedElement":
        stage = report.get("last_allowlisted_stage")
        if not isinstance(stage, str) or stage not in STAGES:
            raise InvalidEvidence("no bounded focus timeout stage")
    elif "last_allowlisted_stage" in report:
        raise InvalidEvidence("unexpected stage outside focused timeout")
    allowed = set(FALSE_FLAGS) | {
        "evidence_class", "probe_mode", "status", "process_id_matches",
        "control_type_present", "semantic_focus_matches", "host_focus_claimed",
        "last_allowlisted_stage", "focus_reject_reason",
    }
    if set(report) - allowed:
        raise InvalidEvidence("unexpected diagnostic fields")
    return {"status": status, "probe_mode": mode, "diagnostic_success": status in SUCCESS,
            "autosport_packaged_uia_qualified": False, "human_tested": False, "nvda_verified": False}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("report", type=Path)
    a = p.parse_args()
    try:
        verdict = validate_report(json.loads(a.report.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_fields))
    except (OSError, ValueError, json.JSONDecodeError):
        print("INVALID_MACHINE_EVIDENCE")
        return 2
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    return 0 if verdict["diagnostic_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
