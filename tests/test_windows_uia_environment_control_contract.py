"""Independent Windows UIA environment control (NOT product/NVDA qualification).

Static assertions run everywhere; the corresponding optional Actions job invokes
the actual Windows/WinForms + cross-process MTA UIA control on a hosted runner.
"""
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" /
          "windows_uia_environment_control.ps1")
SOURCE = SCRIPT.read_text(encoding="utf-8")


def test_control_uses_real_external_uia_provider_not_fixture_only():
    assert "System.Windows.Automation.AutomationElement]::FromHandle(" in SOURCE
    assert "Add-Type -AssemblyName UIAutomationClient" in SOURCE
    assert "'-Sta'" in SOURCE and "'-Mta'" in SOURCE
    assert "CONTROL_UIA_OK" in SOURCE


def test_timeout_is_process_isolated_and_fails_closed():
    assert "$probeProcess.WaitForExit(10000)" in SOURCE
    assert "$probeProcess.Kill($true)" in SOURCE
    assert "'CONTROL_UIA_TIMEOUT'" in SOURCE
    assert "$expectedPass = if ($ProbeMode -eq 'FocusedElement')" in SOURCE
    assert "if ($result.status -cne $expectedPass) { exit 1 }" in SOURCE
    assert "CONTROL_PROBE_UNTRUSTED" in SOURCE
    assert "$probe.process_id_matches -eq $true" in SOURCE


def test_diagnostic_never_certifies_product_or_owner_evidence():
    for marker in (
        "autosport_packaged_uia_qualified = $false",
        "autosport_keyboard_qualified = $false",
        "autosport_emergency_stop_qualified = $false",
        "real_money_execution = $false",
        "human_tested = $false",
        "nvda_verified = $false",
        "whole_product_complete = $false",
    ):
        assert marker in SOURCE
    assert "independent_hosted_windows_uia_control_only" in SOURCE
    assert "exception messages" in SOURCE
