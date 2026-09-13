from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .gui import AUTOMATION_IDS
from .windows_gui import WindowsAutosportApp


_ACTION_BINDINGS = {
    "<Control-o>": "choose_dataset",
    "<Control-r>": "run_replay",
    "<Control-Shift-R>": "repair_workspace",
    "<Control-l>": "live_refresh",
}
_FOCUS_BINDINGS = {
    "<F6>": "tickets",
    "<F7>": "live_quotes",
    "<F8>": "evaluation",
}
_FOCUSABLE_CONTROLS = (
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
)


def summarize_keyboard_contract(
    bindings: dict[str, bool],
    focus_results: dict[str, bool],
    tab_reachable_controls: list[str],
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
        "expected_automation_ids": {
            name: AUTOMATION_IDS[name] for name in _FOCUSABLE_CONTROLS
        },
        "failures": failures,
        "evidence_scope": (
            "in-process packaged Windows GUI keyboard contract: action shortcuts are bound, "
            "F6/F7/F8 focus shortcuts are executed, and critical controls are reachable "
            "through Tk tab traversal; not physical keyboard or NVDA speech proof"
        ),
        "human_tested": False,
        "nvda_verified": False,
        "real_money_execution": False,
    }


def _critical_widgets(app: WindowsAutosportApp) -> dict[str, Any]:
    return {
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
    }


def _tab_reachable_controls(app: WindowsAutosportApp) -> list[str]:
    controls = _critical_widgets(app)
    names_by_widget = {widget: name for name, widget in controls.items()}
    start = app.strategy
    current = start
    seen_widgets: set[Any] = set()
    reachable: list[str] = []
    for _ in range(64):
        if current in seen_widgets:
            break
        seen_widgets.add(current)
        name = names_by_widget.get(current)
        if name is not None:
            reachable.append(name)
        next_widget = current.tk_focusNext()
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
        )
    except Exception as exc:
        report = {
            "status": "FAIL",
            "failures": [f"{type(exc).__name__}: {exc}"],
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
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if report.get("status") == "PASS" else 1
