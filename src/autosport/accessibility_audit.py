from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import tk_uia

from .gui import AUTOMATION_IDS
from .integrity import atomic_write_json
from .windows_gui import WINDOWS_BANKROLL_AUTOMATION_ID, WindowsAutosportApp
from .windows_layout import OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS, WINDOWS_SHELL_AUTOMATION_IDS
from .windows_manual_calculation import WORKBENCH_AUTOMATION_IDS, show_manual_calculation_workbench


_REQUIRED_PATTERNS = {
    AUTOMATION_IDS["choose_dataset"]: {"INVOKE"},
    AUTOMATION_IDS["run_replay"]: {"INVOKE"},
    AUTOMATION_IDS["repair_workspace"]: {"INVOKE"},
    AUTOMATION_IDS["export_evidence"]: {"INVOKE"},
    AUTOMATION_IDS["replay_speed"]: {"VALUE"},
    AUTOMATION_IDS["live_mode"]: {"VALUE"},
    AUTOMATION_IDS["live_refresh"]: {"INVOKE"},
    AUTOMATION_IDS["strategy"]: {"VALUE"},
    AUTOMATION_IDS["research_plan"]: {"INVOKE"},
    AUTOMATION_IDS["tickets"]: set(),
    AUTOMATION_IDS["log"]: {"VALUE"},
    AUTOMATION_IDS["live_quotes"]: set(),
    AUTOMATION_IDS["evaluation"]: set(),
    WINDOWS_BANKROLL_AUTOMATION_ID: {"VALUE"},
    WINDOWS_SHELL_AUTOMATION_IDS["navigation"]: {"VALUE"},
    WINDOWS_SHELL_AUTOMATION_IDS["state"]: {"VALUE"},
    WINDOWS_SHELL_AUTOMATION_IDS["open"]: {"INVOKE"},
    WINDOWS_SHELL_AUTOMATION_IDS["details"]: set(),
    WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_open"]: {"INVOKE"},
    WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_status"]: {"VALUE"},
    WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_readback"]: set(),
    OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["readback"]: set(),
    OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["close"]: {"INVOKE"},
    WORKBENCH_AUTOMATION_IDS["open"]: {"INVOKE"},
    WORKBENCH_AUTOMATION_IDS["operation"]: {"VALUE"},
    WORKBENCH_AUTOMATION_IDS["input"]: {"VALUE"},
    WORKBENCH_AUTOMATION_IDS["calculate"]: {"INVOKE"},
    WORKBENCH_AUTOMATION_IDS["result"]: {"VALUE"},
    WORKBENCH_AUTOMATION_IDS["clear"]: {"INVOKE"},
    WORKBENCH_AUTOMATION_IDS["close"]: {"INVOKE"},
}

_EXPECTED_ROLES = {
    AUTOMATION_IDS["choose_dataset"]: "PUSH_BUTTON",
    AUTOMATION_IDS["run_replay"]: "PUSH_BUTTON",
    AUTOMATION_IDS["repair_workspace"]: "PUSH_BUTTON",
    AUTOMATION_IDS["export_evidence"]: "PUSH_BUTTON",
    AUTOMATION_IDS["replay_speed"]: "COMBO_BOX",
    AUTOMATION_IDS["live_mode"]: "COMBO_BOX",
    AUTOMATION_IDS["live_refresh"]: "PUSH_BUTTON",
    AUTOMATION_IDS["strategy"]: "COMBO_BOX",
    AUTOMATION_IDS["research_plan"]: "PUSH_BUTTON",
    AUTOMATION_IDS["tickets"]: "LIST",
    AUTOMATION_IDS["log"]: "TEXT",
    AUTOMATION_IDS["live_quotes"]: "LIST",
    AUTOMATION_IDS["evaluation"]: "LIST",
    WINDOWS_BANKROLL_AUTOMATION_ID: "TEXT",
    WINDOWS_SHELL_AUTOMATION_IDS["navigation"]: "COMBO_BOX",
    WINDOWS_SHELL_AUTOMATION_IDS["state"]: "TEXT",
    WINDOWS_SHELL_AUTOMATION_IDS["open"]: "PUSH_BUTTON",
    WINDOWS_SHELL_AUTOMATION_IDS["details"]: "LIST",
    WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_open"]: "PUSH_BUTTON",
    WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_status"]: "TEXT",
    WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_readback"]: "LIST",
    OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["readback"]: "LIST",
    OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["close"]: "PUSH_BUTTON",
    WORKBENCH_AUTOMATION_IDS["open"]: "PUSH_BUTTON",
    WORKBENCH_AUTOMATION_IDS["operation"]: "COMBO_BOX",
    WORKBENCH_AUTOMATION_IDS["input"]: "TEXT",
    WORKBENCH_AUTOMATION_IDS["calculate"]: "PUSH_BUTTON",
    WORKBENCH_AUTOMATION_IDS["result"]: "TEXT",
    WORKBENCH_AUTOMATION_IDS["clear"]: "PUSH_BUTTON",
    WORKBENCH_AUTOMATION_IDS["close"]: "PUSH_BUTTON",
}

_ROW_CONTROLS = {
    AUTOMATION_IDS["tickets"],
    AUTOMATION_IDS["live_quotes"],
    AUTOMATION_IDS["evaluation"],
    WINDOWS_SHELL_AUTOMATION_IDS["details"],
    WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_readback"],
    OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["readback"],
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


def _safe_exception_detail(exc: Exception) -> str:
    """Render audit failure evidence without trusting exception formatting."""

    try:
        exception_type = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        exception_type = "Exception"
    try:
        rendered = str.__str__(str(exc))
    except BaseException:
        rendered = "exception details unavailable"
    return f"{exception_type}: {rendered}"


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "name", value))


def _readonly_entry(widget: Any) -> bool:
    if widget is None:
        return False
    return str(widget.cget("state")) == "readonly" and bool(
        widget.instate(("readonly", "!disabled"))
    )


def _bankroll_summary_is_readonly(app: WindowsAutosportApp) -> bool:
    """Bind packaged accessibility evidence to the actual Tk bankroll widget state."""
    return _readonly_entry(getattr(app, "bank_summary", None))


def _shell_state_is_readonly(app: WindowsAutosportApp) -> bool:
    """Bind shell presentation-state evidence to its actual Tk readonly state."""
    return _readonly_entry(getattr(app, "shell_state", None))


def _owner_economic_state_is_readonly(app: WindowsAutosportApp) -> bool:
    """Bind the owner-authority state evidence to the actual Tk readonly widget."""
    return _readonly_entry(getattr(app, "owner_economic_authority_state", None))


def _disabled_text_is_readonly(widget: Any) -> bool:
    if widget is None:
        return False
    return str(widget.cget("state")) == "disabled"


def _combined_description(*descriptions: Any) -> Any:
    if not descriptions:
        raise ValueError("at least one UIA description is required")
    first = descriptions[0]
    return SimpleNamespace(
        strategy=first.strategy,
        widgets=tuple(widget for description in descriptions for widget in description.widgets),
        provider_trouble=tuple(
            item for description in descriptions for item in description.provider_trouble
        ),
        providers_stood_down_because=next(
            (
                description.providers_stood_down_because
                for description in descriptions
                if description.providers_stood_down_because
            ),
            None,
        ),
    )


def _open_owner_economic_dialog_for_audit(app: WindowsAutosportApp) -> Any:
    """Open the real owner-authority dialog without creating or changing economic state."""
    before = set(app.winfo_children())
    app.owner_economic_authority_button.invoke()
    app.update_idletasks()
    app.update()
    created = [
        child
        for child in app.winfo_children()
        if child not in before and child.winfo_toplevel() is child
    ]
    if len(created) != 1:
        raise RuntimeError(
            f"owner economic dialog open created {len(created)} top-level windows; expected 1"
        )
    return created[0]


def summarize_description(
    description: Any,
    *,
    bankroll_readonly: bool | None = None,
    shell_state_readonly: bool | None = None,
    owner_economic_state_readonly: bool | None = None,
    workbench_result_readonly: bool | None = None,
) -> dict[str, Any]:
    expected_ids = set(_REQUIRED_PATTERNS)
    controls: dict[int, dict[str, Any]] = {}
    failures: list[str] = []

    for widget in description.widgets:
        automation_id = widget.automation_id
        if automation_id not in expected_ids:
            continue
        normalized_automation_id = int(automation_id)
        if normalized_automation_id in controls:
            failures.append(
                f"automation_id={normalized_automation_id}: duplicate critical control identity "
                f"first_path={controls[normalized_automation_id]['path']} "
                f"duplicate_path={widget.path}"
            )
            continue
        gap_names = sorted(_enum_name(gap) for gap in widget.gaps if _enum_name(gap))
        pattern_names = sorted(_enum_name(pattern) for pattern in widget.patterns if _enum_name(pattern))
        role_name = _enum_name(widget.role)
        controls[normalized_automation_id] = {
            "path": widget.path,
            "tk_class": widget.tk_class,
            "role": role_name,
            "name": widget.name,
            "automation_id": normalized_automation_id,
            "patterns": pattern_names,
            "answers_rows": bool(widget.answers_rows),
            "gaps": gap_names,
        }
        if not widget.name:
            failures.append(f"automation_id={normalized_automation_id}: missing accessible name")
        expected_role = _EXPECTED_ROLES[normalized_automation_id]
        if role_name != expected_role:
            actual_role = role_name if role_name is not None else "NONE"
            failures.append(
                f"automation_id={normalized_automation_id}: unexpected accessible role={actual_role} expected={expected_role}"
            )
        blockers = sorted(set(gap_names) & _BLOCKING_GAPS)
        if blockers:
            failures.append(
                f"automation_id={normalized_automation_id}: blocking gaps={','.join(blockers)}"
            )
        required = _REQUIRED_PATTERNS[normalized_automation_id]
        missing_patterns = sorted(required - set(pattern_names))
        if missing_patterns:
            failures.append(
                f"automation_id={normalized_automation_id}: missing UIA patterns={','.join(missing_patterns)}"
            )
        if normalized_automation_id in _ROW_CONTROLS and not widget.answers_rows:
            failures.append(
                f"automation_id={normalized_automation_id}: list rows are not exposed through UIA"
            )

    missing_ids = sorted(expected_ids - set(controls))
    for automation_id in missing_ids:
        failures.append(f"automation_id={automation_id}: critical control not found")

    if WINDOWS_BANKROLL_AUTOMATION_ID in controls:
        controls[WINDOWS_BANKROLL_AUTOMATION_ID]["read_only"] = bankroll_readonly is True
        if bankroll_readonly is not True:
            failures.append(
                f"automation_id={WINDOWS_BANKROLL_AUTOMATION_ID}: bankroll summary is not runtime readonly"
            )

    shell_state_id = WINDOWS_SHELL_AUTOMATION_IDS["state"]
    if shell_state_id in controls:
        controls[shell_state_id]["read_only"] = shell_state_readonly is True
        if shell_state_readonly is not True:
            failures.append(
                f"automation_id={shell_state_id}: shell state is not runtime readonly"
            )

    owner_state_id = WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_status"]
    if owner_state_id in controls:
        controls[owner_state_id]["read_only"] = owner_economic_state_readonly is True
        if owner_economic_state_readonly is not True:
            failures.append(
                f"automation_id={owner_state_id}: owner economic state is not runtime readonly"
            )

    workbench_result_id = WORKBENCH_AUTOMATION_IDS["result"]
    if workbench_result_id in controls:
        controls[workbench_result_id]["read_only"] = workbench_result_readonly is True
        if workbench_result_readonly is not True:
            failures.append(
                f"automation_id={workbench_result_id}: manual calculation result is not runtime readonly"
            )

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
        "evidence_scope": (
            "in-process tk-uia annotation/provider audit plus runtime Tk readonly-state audit "
            "of the packaged Windows GUI and canonical product-shell controls, including the "
            "always-present owner-economic dialog readback/close shell; not external UIA client "
            "or NVDA speech proof"
        ),
        "human_tested": False,
        "nvda_verified": False,
        "real_money_execution": False,
    }


def run_accessibility_audit(output_path: str | Path) -> int:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    app: WindowsAutosportApp | None = None
    owner_dialog: Any | None = None
    dialog: Any | None = None
    try:
        app = WindowsAutosportApp()
        app.update_idletasks()
        app.update()
        # Snapshot the already-enabled main window before opening later Toplevels.
        # tk-uia 0.8.0 covers later windows from the original enable(root), while
        # keeping the main-window description independent of Toplevel handle churn.
        root_description = tk_uia.describe(app)
        owner_dialog = _open_owner_economic_dialog_for_audit(app)
        owner_dialog_description = tk_uia.describe(owner_dialog)
        owner_dialog.destroy()
        owner_dialog = None
        app.update_idletasks()
        app.update()
        dialog = show_manual_calculation_workbench(app)
        app.update_idletasks()
        app.update()
        controls = getattr(dialog, "_autosport_workbench_controls", {})
        dialog_description = tk_uia.describe(dialog)
        report = summarize_description(
            _combined_description(
                root_description,
                owner_dialog_description,
                dialog_description,
            ),
            bankroll_readonly=_bankroll_summary_is_readonly(app),
            shell_state_readonly=_shell_state_is_readonly(app),
            owner_economic_state_readonly=_owner_economic_state_is_readonly(app),
            workbench_result_readonly=_disabled_text_is_readonly(controls.get("result")),
        )
    except Exception as exc:
        report = {
            "status": "FAIL",
            "failures": [_safe_exception_detail(exc)],
            "evidence_scope": "accessibility prerequisite audit failed before completion",
            "human_tested": False,
            "nvda_verified": False,
            "real_money_execution": False,
        }
    finally:
        if owner_dialog is not None:
            try:
                owner_dialog.destroy()
            except Exception:
                pass
        if dialog is not None:
            try:
                dialog.destroy()
            except Exception:
                pass
        if app is not None:
            try:
                app.close_app()
            except Exception:
                pass
    atomic_write_json(destination, report)
    return 0 if report.get("status") == "PASS" else 1
