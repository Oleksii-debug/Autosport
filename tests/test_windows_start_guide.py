from __future__ import annotations

import unittest
from pathlib import Path


class WindowsStartGuideTests(unittest.TestCase):
    def test_exposes_evaluation_keyboard_shortcut(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("F6 — paper tickets", guide)
        self.assertIn("F7 — live quotes", guide)
        self.assertIn("F8 — Evaluation і portfolio evidence", guide)


if __name__ == "__main__":
    unittest.main()
