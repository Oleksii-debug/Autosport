"""Source-level regression for independent packaged native STOP keyboard gate.

Passing these tests is NOT Windows/UIA/NVDA acceptance; workflow separately runs
this harness with a real freshly extracted Windows executable and native events.
"""
from __future__ import annotations

from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "native_stop_keyboard_windows_e2e.ps1"

REQUIRED = {
    "absolute_executable": "[IO.Path]::IsPathFullyQualified($Exe)",
    "file_binding": "Get-FileHash -LiteralPath $Exe -Algorithm SHA256",
    "posttest_hash_readback": "throw 'Packaged STOP executable bytes changed during qualification'",
    "fresh_test_root": "Refusing to overwrite a pre-existing native STOP test root",
    "owned_window_pid": "GetWindowThreadProcessId(window, out candidate)",
    "owned_window_title": "String.Equals(text.ToString(), title, StringComparison.Ordinal)",
    "foreground_key_owner": "refusing to send keys to a foreign window",
    "native_keyboard_input": "[System.Windows.Forms.SendKeys]::SendWait($Keys)",
    "native_no_default": "DefaultButtonId($prompt) -eq 7",
    "no_requires_exit_3": "Assert-ProcessExit -Child $child -Expected 3",
    "no_requires_no_journal": "Default-No native STOP unexpectedly wrote a durable journal",
    "yes_requires_exit_0": "Assert-ProcessExit -Child $child -Expected 0",
    "yes_real_keyboard": "-Keys '{LEFT}{ENTER}'",
    "yes_requires_journal": "Native STOP reported success without durable journal",
    "yes_requires_independent_authority_reread": "s=WindowsEmergencyStopBridge.for_workspace(default_workspace()).status()",
    "fail_is_not_pass": "status = 'FAIL'",
    "not_nvda_claim": "physical_nvda = $false",
    "not_real_money_claim": "real_money_execution = $false",
    "webview_uia_not_tested": "webview_uia = 'NOT_TESTED'",
    "bounded_native_send": "SendMessageTimeoutW",
    "no_send_to_foreign_pid": "Get-TrustedPackagedProcessFamily -RootPid $RootPid",
    "same_image_process_check": "[StringComparison]::OrdinalIgnoreCase",
    "native_dialog_class": '"#32770"',
    "cleanup_orphan_process": "Stop-Process -Id ([int]$trustedPid) -Force",
    "cleanup_failed_cim_still_follows_observed_family": "$nowTrusted + $verifiedFamily",
}


def violations(text: str) -> list[str]:
    return [name for name, witness in REQUIRED.items() if witness not in text] + (
        ["forbidden_button_click_bypass"] if "BM_CLICK" in text or "InvokePattern.Invoke" in text else []
    )


class HarnessSourceSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_all_safety_witnesses_exist(self) -> None:
        self.assertEqual(violations(self.source), [])

    def test_remove_no_default_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["native_no_default"], "$true")
        self.assertIn("native_no_default", violations(tampered))

    def test_remove_foreground_guard_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["foreground_key_owner"], "unsafe direct send")
        self.assertIn("foreground_key_owner", violations(tampered))

    def test_remove_stable_journal_confirmation_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["yes_requires_independent_authority_reread"], "True")
        self.assertIn("yes_requires_independent_authority_reread", violations(tampered))

    def test_tamper_real_money_label_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["not_real_money_claim"], "real_money_execution = $true")
        self.assertIn("not_real_money_claim", violations(tampered))

    def test_tamper_nvda_label_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["not_nvda_claim"], "physical_nvda = $true")
        self.assertIn("not_nvda_claim", violations(tampered))

    def test_mock_button_click_is_forbidden(self) -> None:
        self.assertIn("forbidden_button_click_bypass", violations(self.source + "\n# BM_CLICK\n"))

    def test_invalid_yes_journal_skip_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["yes_requires_journal"], "skip journal")
        self.assertIn("yes_requires_journal", violations(tampered))

    def test_missing_executable_hash_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["file_binding"], "Get-Item $Exe")
        self.assertIn("file_binding", violations(tampered))

    def test_unqualified_webview_must_not_be_marked_pass(self) -> None:
        tampered = self.source.replace(REQUIRED["webview_uia_not_tested"], "webview_uia = 'PASS'")
        self.assertIn("webview_uia_not_tested", violations(tampered))

    def test_remove_process_family_guard_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["no_send_to_foreign_pid"], "unsafe all-process enumeration")
        self.assertIn("no_send_to_foreign_pid", violations(tampered))

    def test_remove_posttest_hash_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["posttest_hash_readback"], "return")
        self.assertIn("posttest_hash_readback", violations(tampered))

    def test_remove_bounded_send_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["bounded_native_send"], "unbounded message")
        self.assertIn("bounded_native_send", violations(tampered))

    def test_remove_no_case_exit_check_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["no_requires_exit_3"], "Write-Host inert")
        self.assertIn("no_requires_exit_3", violations(tampered))

    def test_remove_yes_case_exit_check_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["yes_requires_exit_0"], "Write-Host okay")
        self.assertIn("yes_requires_exit_0", violations(tampered))

    def test_remove_orphan_cleanup_must_fail(self) -> None:
        tampered = self.source.replace(REQUIRED["cleanup_failed_cim_still_follows_observed_family"], "$nowTrusted")
        self.assertIn("cleanup_failed_cim_still_follows_observed_family", violations(tampered))


if __name__ == "__main__":
    unittest.main(verbosity=2)
