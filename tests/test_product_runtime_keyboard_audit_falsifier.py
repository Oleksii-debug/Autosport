from __future__ import annotations

from types import SimpleNamespace

import autosport.keyboard_audit as keyboard_audit
from autosport.product_windows_gui import (
    PRODUCT_RUNTIME_AUTOMATION_IDS,
    ProductWindowsAutosportApp,
)


def _fake_packaged_app() -> SimpleNamespace:
    names = (
        "shell_navigation",
        "shell_open_button",
        "shell_state",
        "shell_details",
        "owner_economic_authority_button",
        "owner_economic_authority_state",
        "owner_economic_authority_readback",
        "strategy",
        "research_plan_button",
        "choose_button",
        "run_button",
        "repair_button",
        "speed",
        "live_mode",
        "live_refresh_button",
        "live_quotes",
        "tickets",
        "evaluation",
        "log",
        "bank_summary",
        "manual_calculation_button",
        "product_start_button",
        "product_stop_button",
        "product_status_entry",
    )
    return SimpleNamespace(**{name: object() for name in names})


def test_keyboard_audit_targets_packaged_product_gui_and_runtime_controls() -> None:
    assert issubclass(
        keyboard_audit.WindowsAutosportApp,
        ProductWindowsAutosportApp,
    ), "canonical keyboard audit still instantiates the predecessor Windows GUI"

    app = _fake_packaged_app()
    controls = keyboard_audit._critical_widgets(app)

    product_widgets = {
        app.product_start_button,
        app.product_stop_button,
        app.product_status_entry,
    }
    assert product_widgets <= set(controls.values())

    product_control_names = {
        name for name, widget in controls.items() if widget in product_widgets
    }
    assert len(product_control_names) == 3
    assert product_control_names <= set(keyboard_audit._FOCUSABLE_CONTROLS)

    bindings = {
        sequence: True
        for sequence in (
            *keyboard_audit._ACTION_BINDINGS,
            *keyboard_audit._FOCUS_BINDINGS,
        )
    }
    focus_results = {
        sequence: True for sequence in keyboard_audit._FOCUS_BINDINGS
    }
    reachable = list(keyboard_audit._FOCUSABLE_CONTROLS)
    report = keyboard_audit.summarize_keyboard_contract(
        bindings,
        focus_results,
        reachable,
        list(reversed(reachable)),
    )

    assert report["status"] == "PASS"
    expected_ids = list(report["expected_automation_ids"].values())
    assert set(PRODUCT_RUNTIME_AUTOMATION_IDS.values()) <= set(expected_ids)
    assert len(expected_ids) == len(set(expected_ids)), (
        "critical packaged controls must have unique AutomationIds"
    )
