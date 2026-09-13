from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tk_uia

from .gui import AUTOMATION_IDS, AutosportApp


_REQUIRED_PATTERNS = {
    AUTOMATION_IDS["choose_dataset"]: {"INVOKE"},
    AUTOMATION_IDS["run_replay"]: {"INVOKE"},
    AUTOMATION_IDS["replay_speed"]: {"VALUE"},
    AUTOMATION_IDS["strategy"]: {"VALUE"},
    AUTOMATION_IDS["research_plan"]: {"INVOKE"},
    AUTOMATION_IDS["live_mode"]: {"VALUE"},
    AUTOMATION_IDS["live_refresh"]: {"INVOKE"},
    AUTOMATION_IDS["tickets"]: set(),
    AUTOMATION_IDS["log"]: {"VALUE"},
    AUTOMATION_IDS["live_quotes"]: set(),
}

_ROW_CONTROLS = {
    AUTOMATION_IDS["tickets"],
    AUTOMATION_IDS["live_quotes"],
}

_BLOCKING_GAPS = {
    "NO_ROLE_FOR_ITS_CLASS",
    "NEVER_MAPPED",
    "NOTHING_WRITTEN",
    "ANNOTATED_ON_A_HANDLE_IT_NO_LONGER_HAS",
    "NO_NAME",
    "CANNOT_BE_PRESSED",
    "LEFT_TO_THE_PROXY",
}


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "name", value))


def summarize_description(description: Any) -> dict[str, Any]:
    expected_ids = set(_REQUIRED_PATTERNS)
    controls: dict[int, dict[str, Any]] = {}
    failures: list[str] = []

    for widget in description.widgets:
        automation_id = widget.automation_id
        if automation_id not in expected_ids:
            continue
        gap_names = sorted(_enum_name(gap) for gap in widget.gaps if _enum_name(gap))
        pattern_names = sorted(_enum_name(pattern) for pattern in widget.patterns if _enum_name(pattern))
        controls[int(automation_id)] = {
            "path": widget.path,
            "tk_class": widget.tk_class,
            "role": _enum_name(widget.role),
            "name": widget.name,
            "automation_id": automation_id,
            "patterns": pattern_names,
            "answers_rows": bool(widget.answers_rows),
            "gaps": gap_names,
        }
        if not widget.name:
            failures.append(f"automation_id={automation_id}: missing accessible name")
        if widget.role is None:
            failures.append(f"automation_id={automation_id}: missing accessible role")
        blockers = sorted(set(gap_names) & _BLOCKING_GAPS)
        if blockers:
            failures.append(f"automation_id={automation_id}: blocking gaps={','.join(blockers)}")
        required = _REQUIRED_PATTERNS[int(automation_id)]
        missing_patterns = sorted(required - set(pattern_names))
        if missing_patterns:
            failures.append(
                f"automation_id={automation_id}: missing UIA patterns={','.join(missing_patterns)}"
            )
        if int(automation_id) in _ROW_CONTROLS and not widget.answers_rows:
            failures.append(f"automation_id={automation_id}: list rows are not exposed through UIA")

    missing_ids = sorted(expected_ids - set(controls))
    for automation_id in missing_ids:
        failures.append(f"automation_id={automation_id}: critical control not found")

    provider_trouble = [str(item) for item in description.provider_trouble]
    if provider_trouble:
        failures.extend(f"provider trouble: {item}" for item in provider_trouble)

    return {
        "status": "PASS" if not failures else "FAIL",
        "strategy": _enum_name(description.strategy),
        "critical_controls": [controls[key] for key in sorted(controls)],
        "failures": failures,
        "provider_trouble": provider_trouble,
        "providers_stood_down_because": description.providers_stood_down_because,
        "evidence_scope": "in-process tk-uia annotation/provider audit; not external UIA client or NVDA speech proof",
        "human_tested": False,
        "nvda_verified": False,
        "real_money_execution": False,
    }


def run_accessibility_audit(output_path: str | Path) -> int:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    app: AutosportApp | None = None
    try:
        app = AutosportApp()
        app.update_idletasks()
        app.update()
        report = summarize_description(tk_uia.describe(app))
    except Exception as exc:
        report = {
            "status": "FAIL",
            "failures": [f"{type(exc).__name__}: {exc}"],
            "evidence_scope": "accessibility prerequisite audit failed before completion",
            "human_tested": False,
            "nvda_verified": False,
            "real_money_execution": False,
        }
    finally:
        if app is not None:
            try:
                app.close_app()
            except Exception:
                pass
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report.get("status") == "PASS" else 1
