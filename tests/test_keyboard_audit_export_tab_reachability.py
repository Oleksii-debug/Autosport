from __future__ import annotations

from types import SimpleNamespace

import autosport.keyboard_audit as keyboard_audit
from autosport.gui import AUTOMATION_IDS


def _app_with_export_button(export_button: object) -> SimpleNamespace:
    return SimpleNamespace(
        shell_navigation=object(),
        shell_open_button=object(),
        shell_state=object(),
        shell_details=object(),
        owner_economic_authority_button=object(),
        owner_economic_authority_state=object(),
        owner_economic_authority_readback=object(),
        strategy=object(),
        research_plan_button=object(),
        choose_button=object(),
        run_button=object(),
        export_evidence_button=export_button,
        repair_button=object(),
        speed=object(),
        live_mode=object(),
        live_refresh_button=object(),
        live_quotes=object(),
        tickets=object(),
        evaluation=object(),
        log_accessible=object(),
        bank_summary=object(),
        manual_calculation_button=object(),
    )


def test_export_evidence_is_part_of_bidirectional_tab_contract() -> None:
    assert "export_evidence" in keyboard_audit._FOCUSABLE_CONTROLS

    bindings = {
        sequence: True
        for sequence in (*keyboard_audit._ACTION_BINDINGS, *keyboard_audit._FOCUS_BINDINGS)
    }
    focus = {sequence: True for sequence in keyboard_audit._FOCUS_BINDINGS}
    reachable = list(keyboard_audit._FOCUSABLE_CONTROLS)
    report = keyboard_audit.summarize_keyboard_contract(
        bindings,
        focus,
        reachable,
        list(reversed(reachable)),
    )

    assert report["status"] == "PASS"
    assert report["expected_automation_ids"]["export_evidence"] == 109
    assert AUTOMATION_IDS["export_evidence"] == 109


def test_tab_probe_maps_export_evidence_to_real_export_button() -> None:
    export_button = object()
    controls = keyboard_audit._critical_widgets(_app_with_export_button(export_button))

    assert controls["export_evidence"] is export_button
