from __future__ import annotations

from pathlib import Path

from autosport.nvda_acceptance import _REQUIRED_CHECKS


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_physical_nvda_required_check_covers_current_keyboard_first_surfaces() -> None:
    checks = dict(_REQUIRED_CHECKS)
    evidence_surfaces = checks["evidence_surfaces"]

    for required_token in (
        "F2 shell navigation",
        "Ctrl+Alt+Left/Right",
        "F6/F7/F8",
        "F9 owner-economic authority",
        "F10 manual-calculation workbench/dialog",
        "returns focus predictably",
    ):
        assert required_token in evidence_surfaces


def test_windows_start_guide_exposes_same_current_surface_journey_without_promoting_truth() -> None:
    guide = (REPO_ROOT / "WINDOWS_START_HERE.txt").read_text(encoding="utf-8")

    for required_token in (
        "Ctrl+Shift+R",
        "F2 — shell navigation",
        "Ctrl+Alt+Left/Right",
        "F6 — paper tickets",
        "F7 — live quotes",
        "F8 — Evaluation",
        "F9 — owner-economic authority",
        "F10 — manual-calculation workbench",
        "F2 має перевести фокус на shell navigation",
        "F9 має відкрити/сфокусувати owner-economic authority surface",
        "F10 має відкрити manual-calculation workbench/dialog",
        "фокус повинен повернутися у зрозумілий keyboard flow",
    ):
        assert required_token in guide

    assert "HUMAN_TESTED=false" in guide
    assert "NVDA_VERIFIED=false" in guide
    assert "REAL_MONEY_EXECUTION=false" in guide
