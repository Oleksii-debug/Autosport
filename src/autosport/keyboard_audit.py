from __future__ import annotations

from pathlib import Path
from typing import Any

from .gui import AUTOMATION_IDS
from .integrity import atomic_write_json
# This module is the retired Tk audit kept for historical/unit evidence only.
# The canonical packaged WebView2 keyboard audit lives in windows_webview_audit.
_STARTUP_FOCUS_CONTROL = "shell_navigation"
from .windows_gui import WINDOWS_BANKROLL_AUTOMATION_ID, WindowsAutosportApp
from .windows_layout import WINDOWS_SHELL_AUTOMATION_IDS
from .windows_manual_calculation import WORKBENCH_AUTOMATION_IDS, show_manual_calculation_workbench


_ACTION_BINDINGS = {
    "<Control-o>": "choose_dataset",
    "<Control-r>": "run_replay",
    "<Control-Shift-R>": "repair_workspace",
    "<Control-l>": "live_refresh",
    "<Control-Alt-Left>": "shell_previous",
    "<Control-Alt-Right>": "shell_next",
}
_FOCUS_BINDINGS = {
    "<F2>": "shell_navigation",
    "<F9>": "owner_economic_open",
    "<F10>": "manual_calculation_open",
    "<F6>": "tickets",
    "<F7>": "live_quotes",
    "<F8>": "evaluation",
}
_FOCUSABLE_CONTROLS = (
    "shell_navigation",
    "shell_open",
    "shell_state",
    "shell_details",
    "owner_economic_open",
    "owner_economic_status",
    "owner_economic_readback",
    "manual_calculation_open",
    "manual_calculation_operation",
    "manual_calculation_input",
    "manual_calculation_calculate",
    "manual_calculation_result",
    "manual_calculation_clear",
    "manual_calculation_close",
    "strategy",
    "research_plan",
    "choose_dataset",
    "run_replay",
    "repair_workspace",
    "replay_speed",
    "live_mode",
    "live_refresh",
    "live_quotes",
    "tickets",
    "evaluation",
    "log",
    "bankroll",
)


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


def summarize_keyboard_contract(
    bindings: dict[str, bool],
    focus_results: dict[str, bool],
    tab_reachable_controls: list[str],
    reverse_tab_reachable_controls: list[str] | None = None,
    *,
    startup_focus_control: str | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    startup_focus_passed = startup_focus_control == _STARTUP_FOCUS_CONTROL
    if not startup_focus_passed:
        failures.append(
            "startup focus expected "
            f"{_STARTUP_FOCUS_CONTROL}, observed {startup_focus_control or '<none>'}"
        )
    for sequence in (*_ACTION_BINDINGS, *_FOCUS_BINDINGS):
        if not bindings.get(sequence, False):
            failures.append(f"{sequence}: keyboard binding missing")
    for sequence, control in _FOCUS_BINDINGS.items():
        if not focus_results.get(sequence, False):
            failures.append(f"{sequence}: did not move focus to {control}")
    missing_tab = [name for name in _FOCUSABLE_CONTROLS if name not in tab_reachable_controls]
    if missing_tab:
        failures.append("Tab traversal cannot reach: " + ", ".join(missing_tab))
    if reverse_tab_reachable_controls is None:
        failures.append("Shift+Tab traversal evidence missing")
    else:
        missing_reverse_tab = [
            name for name in _FOCUSABLE_CONTROLS if name not in reverse_tab_reachable_controls
        ]
        if missing_reverse_tab:
            failures.append("Shift+Tab traversal cannot reach: " + ", ".join(missing_reverse_tab))

    shell_ids = WINDOWS_SHELL_AUTOMATION_IDS
    workbench_names = {
        "manual_calculation_open": "open",
        "manual_calculation_operation": "operation",
        "manual_calculation_input": "input",
        "manual_calculation_calculate": "calculate",
        "manual_calculation_result": "result",
        "manual_calculation_clear": "clear",
        "manual_calculation_close": "close",
    }

    def automation_id_for(name: str) -> int:
        if name == "bankroll":
            return WINDOWS_BANKROLL_AUTOMATION_ID
        if name in workbench_names:
            return WORKBENCH_AUTOMATION_IDS[workbench_names[name]]
        if name.startswith("shell_") or name.startswith("owner_economic_"):
            return shell_ids[
                {
                    "shell_navigation": "navigation",
                    "shell_open": "open",
                    "shell_state": "state",
                    "shell_details": "details",
                    "owner_economic_open": "owner_economic_open",
                    "owner_economic_status": "owner_economic_status",
                    "owner_economic_readback": "owner_economic_readback",
                }[name]
            ]
        return AUTOMATION_IDS[name]

    expected_ids = {name: automation_id_for(name) for name in _FOCUSABLE_CONTROLS}
    return {
        "status": "PASS" if not failures else "FAIL",
        "startup_focus": {
            "expected_control": _STARTUP_FOCUS_CONTROL,
            "observed_control": startup_focus_control,
            "passed": startup_focus_passed,
        },
        "action_shortcuts_bound": {
            sequence: bool(bindings.get(sequence, False)) for sequence in _ACTION_BINDINGS
        },
        "focus_shortcuts_executed": {
            sequence: {
                "target": target,
                "passed": bool(focus_results.get(sequence, False)),
            }
            for sequence, target in _FOCUS_BINDINGS.items()
        },
        "tab_reachable_controls": list(tab_reachable_controls),
        "shift_tab_reachable_controls": (
            [] if reverse_tab_reachable_controls is None else list(reverse_tab_reachable_controls)
        ),
        "expected_automation_ids": expected_ids,
        "failures": failures,
        "evidence_scope": (
            "in-process packaged Windows GUI keyboard contract: actual first focus is sampled before any "
            "audit-induced focus change, action shortcuts and shell cycling are bound, F2/F6/F7/F8/F9/F10 "
            "focus shortcuts are executed, and critical shell controls plus the manual calculation workbench "
            "are reachable through forward Tab and reverse Shift+Tab traversal; not physical keyboard or NVDA "
            "speech proof"
        ),
        "human_tested": False,
        "nvda_verified": False,
        "real_money_execution": False,
    }


def _critical_widgets(
    app: WindowsAutosportApp,
    workbench_dialog: Any | None = None,
) -> dict[str, Any]:
    controls = {
        "shell_navigation": app.shell_navigation,
        "shell_open": app.shell_open_button,
        "shell_state": app.shell_state,
        "shell_details": app.shell_details,
        "owner_economic_open": app.owner_economic_authority_button,
        "owner_economic_status": app.owner_economic_authority_state,
        "owner_economic_readback": app.owner_economic_authority_readback,
        "strategy": app.strategy,
        "research_plan": app.research_plan_button,
        "choose_dataset": app.choose_button,
        "run_replay": app.run_button,
        "repair_workspace": app.repair_button,
        "replay_speed": app.speed,
        "live_mode": app.live_mode,
        "live_refresh": app.live_refresh_button,
        "live_quotes": app.live_quotes,
        "tickets": app.tickets,
        "evaluation": app.evaluation,
        "log": app.log,
        "bankroll": app.bank_summary,
        "manual_calculation_open": app.manual_calculation_button,
    }
    if workbench_dialog is not None:
        workbench = getattr(workbench_dialog, "_autosport_workbench_controls", {})
        for key in ("operation", "input", "calculate", "result", "clear", "close"):
            widget = workbench.get(key)
            if widget is not None:
                controls[f"manual_calculation_{key}"] = widget
    return controls


def _focused_control_name(app: WindowsAutosportApp) -> str | None:
    focused = app.focus_get()
    for control_name, widget in _critical_widgets(app).items():
        if focused is widget:
            return control_name
    return None


def _tab_reachable_controls(
    app: WindowsAutosportApp,
    *,
    reverse: bool = False,
    workbench_dialog: Any | None = None,
) -> list[str]:
    controls = _critical_widgets(app, workbench_dialog)
    names_by_widget = {widget: name for name, widget in controls.items()}

    def walk(start: Any) -> list[str]:
        current = start
        seen_widgets: set[Any] = set()
        result: list[str] = []
        for _ in range(120):
            if current in seen_widgets:
                break
            seen_widgets.add(current)
            name = names_by_widget.get(current)
            if name is not None:
                result.append(name)
            next_widget = current.tk_focusPrev() if reverse else current.tk_focusNext()
            if next_widget is None:
                break
            current = next_widget
        return result

    reachable = walk(app.shell_navigation)
    if workbench_dialog is not None:
        workbench = getattr(workbench_dialog, "_autosport_workbench_controls", {})
        start_key = "close" if reverse else "operation"
        start = workbench.get(start_key)
        if start is not None:
            reachable.extend(walk(start))
    return list(dict.fromkeys(reachable))


def _binding_presence(app: WindowsAutosportApp) -> dict[str, bool]:
    return {
        sequence: bool(str(app.bind(sequence) or "").strip())
        for sequence in (*_ACTION_BINDINGS, *_FOCUS_BINDINGS)
    }


def _execute_focus_shortcuts(
    app: WindowsAutosportApp,
    workbench_dialog: Any | None = None,
) -> dict[str, bool]:
    controls = _critical_widgets(app, workbench_dialog)
    results: dict[str, bool] = {}
    app.strategy.focus_set()
    app.update()
    for sequence, target in _FOCUS_BINDINGS.items():
        app.event_generate(sequence)
        app.update()
        results[sequence] = app.focus_get() is controls[target]
        app.strategy.focus_set()
        app.update()
    return results


def run_keyboard_audit(output_path: str | Path) -> int:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    app: WindowsAutosportApp | None = None
    dialog: Any | None = None
    try:
        app = WindowsAutosportApp()
        app.update_idletasks()
        app.update()
        startup_focus_control = _focused_control_name(app)
        dialog = show_manual_calculation_workbench(app)
        app.update_idletasks()
        app.update()
        report = summarize_keyboard_contract(
            _binding_presence(app),
            _execute_focus_shortcuts(app, dialog),
            _tab_reachable_controls(app, workbench_dialog=dialog),
            _tab_reachable_controls(app, reverse=True, workbench_dialog=dialog),
            startup_focus_control=startup_focus_control,
        )
    except Exception as exc:
        report = {
            "status": "FAIL",
            "failures": [_safe_exception_detail(exc)],
            "evidence_scope": "keyboard prerequisite audit failed before completion",
            "human_tested": False,
            "nvda_verified": False,
            "real_money_execution": False,
        }
    finally:
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
