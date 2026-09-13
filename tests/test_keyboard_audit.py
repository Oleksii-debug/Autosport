import unittest

from autosport.keyboard_audit import summarize_keyboard_contract


class KeyboardAuditTests(unittest.TestCase):
    def _passing(self):
        bindings = {
            "<Control-o>": True,
            "<Control-r>": True,
            "<Control-l>": True,
            "<F6>": True,
            "<F7>": True,
        }
        focus = {"<F6>": True, "<F7>": True}
        reachable = [
            "choose_dataset",
            "run_replay",
            "replay_speed",
            "live_mode",
            "live_refresh",
            "live_quotes",
            "tickets",
            "log",
        ]
        return bindings, focus, reachable

    def test_keyboard_contract_passes_without_claiming_nvda(self):
        report = summarize_keyboard_contract(*self._passing())
        self.assertEqual(report["status"], "PASS")
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
        focus["<F7>"] = False
        report = summarize_keyboard_contract(bindings, focus, reachable)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("<F7>" in item for item in report["failures"]))

    def test_all_critical_controls_must_be_tab_reachable(self):
        bindings, focus, reachable = self._passing()
        reachable.remove("live_refresh")
        report = summarize_keyboard_contract(bindings, focus, reachable)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("live_refresh" in item for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
