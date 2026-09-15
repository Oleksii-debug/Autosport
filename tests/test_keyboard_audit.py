import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.keyboard_audit as keyboard_audit
from autosport.keyboard_audit import summarize_keyboard_contract
from autosport.windows_gui import WINDOWS_BANKROLL_AUTOMATION_ID


class KeyboardAuditTests(unittest.TestCase):
    def _passing(self):
        bindings = {
            "<Control-o>": True,
            "<Control-r>": True,
            "<Control-Shift-R>": True,
            "<Control-l>": True,
            "<F6>": True,
            "<F7>": True,
            "<F8>": True,
        }
        focus = {"<F6>": True, "<F7>": True, "<F8>": True}
        reachable = [
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
        ]
        return bindings, focus, reachable

    def test_keyboard_contract_passes_without_claiming_nvda(self):
        report = summarize_keyboard_contract(*self._passing())
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["expected_automation_ids"]["bankroll"], WINDOWS_BANKROLL_AUTOMATION_ID)
        self.assertFalse(report["human_tested"])
        self.assertFalse(report["nvda_verified"])
        self.assertFalse(report["real_money_execution"])

    def test_missing_action_binding_fails_closed(self):
        bindings, focus, reachable = self._passing()
        bindings["<Control-r>"] = False
        report = summarize_keyboard_contract(bindings, focus, reachable)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("<Control-r>" in item for item in report["failures"]))

    def test_focus_shortcut_must_reach_exact_target(self):
        bindings, focus, reachable = self._passing()
        focus["<F8>"] = False
        report = summarize_keyboard_contract(bindings, focus, reachable)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("<F8>" in item for item in report["failures"]))

    def test_all_critical_controls_must_be_tab_reachable(self):
        bindings, focus, reachable = self._passing()
        reachable.remove("evaluation")
        report = summarize_keyboard_contract(bindings, focus, reachable)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("evaluation" in item for item in report["failures"]))

    def test_bankroll_summary_must_be_tab_reachable(self):
        bindings, focus, reachable = self._passing()
        reachable.remove("bankroll")
        report = summarize_keyboard_contract(bindings, focus, reachable)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("bankroll" in item for item in report["failures"]))

    def test_machine_evidence_publication_failure_preserves_existing_file(self):
        class _AuditApp:
            def update_idletasks(self):
                return None

            def update(self):
                return None

            def close_app(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "keyboard-audit.json"
            original = '{"status":"PREVIOUS"}\n'
            destination.write_text(original, encoding="utf-8")
            nonfinite_report = {
                "status": "PASS",
                "probe": float("nan"),
                "human_tested": False,
                "nvda_verified": False,
                "real_money_execution": False,
            }

            with (
                patch.object(keyboard_audit, "WindowsAutosportApp", return_value=_AuditApp()),
                patch.object(keyboard_audit, "_binding_presence", return_value={}),
                patch.object(keyboard_audit, "_execute_focus_shortcuts", return_value={}),
                patch.object(keyboard_audit, "_tab_reachable_controls", return_value=[]),
                patch.object(
                    keyboard_audit,
                    "summarize_keyboard_contract",
                    return_value=nonfinite_report,
                ),
            ):
                with self.assertRaises(ValueError):
                    keyboard_audit.run_keyboard_audit(destination)

            self.assertEqual(destination.read_text(encoding="utf-8"), original)
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
