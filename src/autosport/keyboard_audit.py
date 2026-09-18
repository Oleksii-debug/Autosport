from __future__ import annotations

from pathlib import Path
from typing import Any

from .gui import AUTOMATION_IDS
from .integrity import atomic_write_json
from .windows_gui import WINDOWS_BANKROLL_AUTOMATION_ID, WindowsAutosportApp
from .windows_layout import WINDOWS_SHELL_AUTOMATION_IDS


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
    "<F6>": "tickets",
    "<F7>": "live_quotes",
    "<F8>": "evaluation",
}
_FOCUSABLE_CONTROLS = (
    "shell_navigation",
    "shell_open",
    "shell_state",
    "shell_details",
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
) -> dict[str, Any]:
    failures: list[str] = []
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
    expected_ids = {
        name: (
            WINDOWS_BANKROLL_AUTOMATION_ID
            if name == "bankroll"
            else shell_ids[
                {
                    "shell_navigation": "navigation",
                    "shell_open": "open",
                    "shell_state": "state",
                    "shell_details": "details",
                }[name]
            ]
            if name.startswith("shell_")
            else AUTOMATION_IDS[name]
        )
        for name in _FOCUSABLE_CONTROLS
    }
    return {
        "status": "PASS" if not failures else "FAIL",
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
            "in-process packaged Windows GUI keyboard contract: action shortcuts and shell cycling are bound, "
            "F2/F6/F7/F8 focus shortcuts are executed, and critical shell plus V1 controls including the "
            "shell Open action and read-only bankroll summary are reachable through forward Tab and reverse "
            "Shift+Tab traversal; not physical keyboard or NVDA speech proof"
        ),
        "human_tested": False,
        "nvda_verified": False,
        "real_money_execution": False,
    }


def _critical_widgets(app: WindowsAutosportApp) -> dict[str, Any]:
    return {
        "shell_navigation": app.shell_navigation,
        "shell_open": app.shell_open_button,
        "shell_state": app.shell_state,
        "shell_details": app.shell_details,
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
    }


def _tab_reachable_controls(app: WindowsAutosportApp, *, reverse: bool = False) -> list[str]:
    controls = _critical_widgets(app)
    names_by_widget = {widget: name for name, widget in controls.items()}
    start = app.shell_navigation
    current = start
    seen_widgets: set[Any] = set()
    reachable: list[str] = []
    for _ in range(80):
        if current in seen_widgets:
            break
        seen_widgets.add(current)
        name = names_by_widget.get(current)
        if name is not None:
            reachable.append(name)
        next_widget = current.tk_focusPrev() if reverse else current.tk_focusNext()
        if next_widget is None:
            break
        current = next_widget
    return reachable


def _binding_presence(app: WindowsAutosportApp) -> dict[str, bool]:
    return {
        sequence: bool(str(app.bind(sequence) or "").strip())
        for sequence in (*_ACTION_BINDINGS, *_FOCUS_BINDINGS)
    }


def _execute_focus_shortcuts(app: WindowsAutosportApp) -> dict[str, bool]:
    controls = _critical_widgets(app)
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
    try:
        app = WindowsAutosportApp()
        app.update_idletasks()
        app.update()
        report = summarize_keyboard_contract(
            _binding_presence(app),
            _execute_focus_shortcuts(app),
            _tab_reachable_controls(app),
            _tab_reachable_controls(app, reverse=True),
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
        if app is not None:
            try:
                app.close_app()
            except Exception:
                pass
    atomic_write_json(destination, report)
    return 0 if report.get("status") == "PASS" else 1
