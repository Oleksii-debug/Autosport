from __future__ import annotations

import unittest
from pathlib import Path


class WindowsStartGuideTests(unittest.TestCase):
    def test_exposes_evaluation_keyboard_shortcut(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("F6 — paper tickets", guide)
        self.assertIn("F7 — live quotes", guide)
        self.assertIn("F8 — Evaluation і portfolio evidence", guide)

    def test_physical_nvda_acceptance_cannot_be_replaced_by_machine_audit(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("PHYSICAL WINDOWS + NVDA ACCEPTANCE", guide)
        self.assertIn("Ctrl+O, Ctrl+R, Ctrl+L, F6, F7 і F8", guide)
        self.assertIn("Machine UIA/keyboard audit", guide)
        self.assertIn("фізичній Windows + NVDA", guide)
        self.assertIn("HUMAN_TESTED=false", guide)
        self.assertIn("NVDA_VERIFIED=false", guide)
        self.assertIn("V1_READY=false", guide)


if __name__ == "__main__":
    unittest.main()
