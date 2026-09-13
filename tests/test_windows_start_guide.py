from __future__ import annotations

import unittest
from pathlib import Path


class WindowsStartGuideTests(unittest.TestCase):
    def test_exposes_evaluation_keyboard_shortcut(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("F6 — paper tickets", guide)
        self.assertIn("F7 — live quotes", guide)
        self.assertIn("F8 — Evaluation і portfolio evidence", guide)

    def test_exposes_physical_windows_nvda_acceptance_gate_without_upgrading_truth(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("PHYSICAL WINDOWS + NVDA ACCEPTANCE — HUMAN-ONLY GATE", guide)
        self.assertIn("Tab і Shift+Tab", guide)
        self.assertIn("Ctrl+O", guide)
        self.assertIn("Ctrl+R", guide)
        self.assertIn("Ctrl+L", guide)
        self.assertIn("F6", guide)
        self.assertIn("F7", guide)
        self.assertIn("F8", guide)
        self.assertIn("fail-closed помилку", guide)
        self.assertIn("repair-workspace", guide)
        self.assertIn("exact release/source SHA", guide)
        self.assertIn("HUMAN_TESTED=false", guide)
        self.assertIn("NVDA_VERIFIED=false", guide)
        self.assertIn("V1_READY=false", guide)


if __name__ == "__main__":
    unittest.main()
