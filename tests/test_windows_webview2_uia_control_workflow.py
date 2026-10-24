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
    assert "$evidence.autosport_product_uia_pass -ne $false" in step
    assert "$evidence.human_tested -ne $false" in step
    assert "$evidence.nvda_verified -ne $false" in step
    assert "$evidence.real_money_execution -ne $false" in step
