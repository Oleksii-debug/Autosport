from __future__ import annotations

import unittest
from pathlib import Path


class WindowsStartGuideTests(unittest.TestCase):
    def test_exposes_evaluation_keyboard_shortcut(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("F6 — paper tickets", guide)
        self.assertIn("F7 — live quotes", guide)
        self.assertIn("F8 — Evaluation і portfolio evidence", guide)

    def test_binds_physical_nvda_gate_to_exact_packaged_candidate(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("PHYSICAL WINDOWS 11 + NVDA ACCEPTANCE — HUMAN-ONLY RELEASE GATE", guide)
        self.assertIn("SHA-256 зовнішнього release ZIP", guide)
        self.assertIn("`source_sha`", guide)
        self.assertIn("`autosport_exe_sha256`", guide)
        self.assertIn("fresh extraction", guide)
        self.assertIn("Windows edition/build", guide)
        self.assertIn("NVDA version", guide)
        self.assertIn("PASS/FAIL для пунктів 24–29", guide)
        self.assertIn("HUMAN_TESTED=false", guide)
        self.assertIn("NVDA_VERIFIED=false", guide)
        self.assertIn("V1_READY=false", guide)
        self.assertIn("build pipeline не може сам собі приписати human proof", guide)


if __name__ == "__main__":
    unittest.main()
