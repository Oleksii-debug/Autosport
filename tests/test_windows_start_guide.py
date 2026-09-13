from __future__ import annotations

from pathlib import Path


def test_windows_start_guide_exposes_evaluation_keyboard_shortcut() -> None:
    guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

    assert "F6 — paper tickets" in guide
    assert "F7 — live quotes" in guide
    assert "F8 — Evaluation і portfolio evidence" in guide
