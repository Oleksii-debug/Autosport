from __future__ import annotations

import unittest
from pathlib import Path


class WindowsStartGuideTests(unittest.TestCase):
    def test_exposes_evaluation_keyboard_shortcut(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("F6 — paper tickets", guide)
        self.assertIn("F7 — live quotes", guide)
        self.assertIn("F8 — Evaluation і portfolio evidence", guide)

    def test_uses_stage_neutral_ukrainian_operator_headings(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertTrue(
            guide.startswith(
                "АВТОСПОРТ — WINDOWS: ПАПЕРОВЕ МОДЕЛЮВАННЯ ТА РИНКОВА ЛАБОРАТОРІЯ"
            )
        )
        for heading in (
            "ПОРТАТИВНІ ІНСТРУМЕНТИ ДАНИХ ТА ДОСЛІДЖЕННЯ",
            "ПОКРОКОВЕ ПРИЧИННЕ ОЦІНЮВАННЯ",
            "ПОРІВНЯННЯ СТРАТЕГІЙ",
            "ВІДНОВЛЕННЯ ПІСЛЯ ЗБОЮ ТА ЕКОНОМІЧНОЇ РОБОЧОЇ ОБЛАСТІ",
            "ФІЗИЧНА ПЕРЕВІРКА WINDOWS 11 + NVDA — ЛИШЕ ЛЮДИНОЮ",
        ):
            self.assertIn(heading, guide)

        for obsolete in (
            "V1 WINDOWS PAPER / MARKET LAB",
            "PORTABLE DATA / RESEARCH TOOLS",
            "WALK-FORWARD EVALUATION",
            "STRATEGY COMPARISON",
            "CRASH / ECONOMIC WORKSPACE RECOVERY",
            "HUMAN-ONLY RELEASE GATE",
        ):
            self.assertNotIn(obsolete, guide)

    def test_binds_physical_nvda_gate_to_exact_packaged_candidate(self) -> None:
        guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

        self.assertIn("ФІЗИЧНА ПЕРЕВІРКА WINDOWS 11 + NVDA — ЛИШЕ ЛЮДИНОЮ", guide)
        self.assertIn("незалежного GitHub Actions/control record", guide)
        self.assertIn("опублікований SHA-256 release ZIP", guide)
        self.assertIn("`source_sha`", guide)
        self.assertIn("`autosport_exe_sha256`", guide)
        self.assertIn("fresh extraction", guide)
        self.assertIn("--expected-source-sha", guide)
        self.assertIn("--expected-package-sha256", guide)
        self.assertIn("Windows edition/build", guide)
        self.assertIn("NVDA version", guide)
        self.assertIn("PASS/FAIL для пунктів 24–29", guide)
        self.assertIn("HUMAN_TESTED=false", guide)
        self.assertIn("NVDA_VERIFIED=false", guide)
        self.assertIn("V1_READY=false", guide)
        self.assertIn("build pipeline або ZIP не можуть самі собі приписати human proof", guide)


if __name__ == "__main__":
    unittest.main()
