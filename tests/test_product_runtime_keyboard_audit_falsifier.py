from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import autosport.keyboard_audit as base_keyboard_audit
from autosport import windows_entry
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


def test_packaged_keyboard_entrypoint_gates_product_runtime_controls(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def fake_canonical_run(output_path: str | Path) -> int:
        app = _fake_packaged_app()
        controls = base_keyboard_audit._critical_widgets(app)
        product_widgets = {
            app.product_start_button,
            app.product_stop_button,
            app.product_status_entry,
        }
        product_control_names = {
            name for name, widget in controls.items() if widget in product_widgets
        }

        bindings = {
            sequence: True
            for sequence in (
                *base_keyboard_audit._ACTION_BINDINGS,
                *base_keyboard_audit._FOCUS_BINDINGS,
            )
        }
        focus_results = {
            sequence: True for sequence in base_keyboard_audit._FOCUS_BINDINGS
        }
        reachable = list(base_keyboard_audit._FOCUSABLE_CONTROLS)
        report = base_keyboard_audit.summarize_keyboard_contract(
            bindings,
            focus_results,
            reachable,
            list(reversed(reachable)),
        )

        observed.update(
            {
                "path": Path(output_path),
                "app_class": base_keyboard_audit.WindowsAutosportApp,
                "product_control_names": product_control_names,
                "focusable": set(base_keyboard_audit._FOCUSABLE_CONTROLS),
                "report": report,
            }
        )
        return 0

    monkeypatch.setattr(
        base_keyboard_audit,
        "run_keyboard_audit",
        fake_canonical_run,
    )
    destination = tmp_path / "keyboard.json"
    assert windows_entry.main(["--keyboard-audit-output", str(destination)]) == 0

    assert observed["path"] == destination
    app_class = observed["app_class"]
    assert isinstance(app_class, type)
    assert issubclass(app_class, ProductWindowsAutosportApp)

    product_control_names = observed["product_control_names"]
    assert isinstance(product_control_names, set)
    assert len(product_control_names) == 3
    assert product_control_names <= observed["focusable"]

    report = observed["report"]
    assert isinstance(report, dict)
    assert report["status"] == "PASS"
    expected_ids = list(report["expected_automation_ids"].values())
    assert set(PRODUCT_RUNTIME_AUTOMATION_IDS.values()) <= set(expected_ids)
    assert len(expected_ids) == len(set(expected_ids)), (
        "critical packaged controls must have unique AutomationIds"
    )
