from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/windows-webview2-uia-control.yml"


def test_windowed_control_uses_terminal_semantic_report_not_bootloader_exit() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    step = source.split(
        "- name: Probe minimal WebView2 with killable independent UIA client", 1
    )[1].split("- name: Preserve fixed diagnostic codes even on failure", 1)[0]
    assert "& ./dist/Autosport-WebView2-UIA-Control.exe run $reportPath" in step
    assert "$launcherExit" not in step
    assert "$observed -cne 'SEMANTIC_BUTTON_RETURNED'" in step
    assert "$evidence.exact_automation_id -ne $true" in step
    assert "$evidence.autosport_product_uia_pass -ne $false" in step
    assert "$evidence.human_tested -ne $false" in step
    assert "$evidence.nvda_verified -ne $false" in step
    assert "$evidence.real_money_execution -ne $false" in step


def test_minimal_uia_semantic_success_requires_exact_button_identity() -> None:
    """Name/role alone must never make a diagnostic semantic result green."""
    import importlib.util

    source_path = ROOT / "scripts/windows_webview2_uia_control.py"
    spec = importlib.util.spec_from_file_location("autosport_uia_control_identity", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.classify(7, {7}, "ControlType.Button", module.BUTTON_NAME, module.BUTTON_ID) == "SEMANTIC_BUTTON_RETURNED"
    assert module.classify(7, {7}, "ControlType.Button", module.BUTTON_NAME, "wrong-id") == "SEMANTIC_MISMATCH"
    source = source_path.read_text(encoding="utf-8")
    assert "if ($exactId -ne '1')" in source
    assert 'code == "SEMANTIC_BUTTON_RETURNED" and exact == "1"' in source
