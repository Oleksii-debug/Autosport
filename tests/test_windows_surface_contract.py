from __future__ import annotations

import json

from autosport.windows_surface_contract import (
    DEFAULT_SURFACE_KEY,
    SURFACE_BY_KEY,
    SURFACES,
    load_surface_selection,
    save_surface_selection,
    surface_detail_lines,
)


def test_complete_windows_surface_inventory_is_stable_and_truthful() -> None:
    assert [surface.key for surface in SURFACES] == [
        "home_dashboard",
        "market_mirror",
        "research_agents",
        "opportunities",
        "portfolio",
        "paper_bank",
        "tickets_positions",
        "evaluation_learning",
        "bookmakers_accounts",
        "history_results",
        "settings",
        "diagnostics_recovery",
        "help_about",
    ]
    assert len(SURFACE_BY_KEY) == len(SURFACES)
    assert all(
        surface.title_uk
        and any("А" <= char <= "я" or char in "ІіЇїЄєҐґ" for char in surface.title_uk)
        for surface in SURFACES
    )
    assert all(surface.authority_uk for surface in SURFACES)
    assert all(surface.accessibility_uk for surface in SURFACES)
    assert all(surface.persistence_uk for surface in SURFACES)
    assert all(surface.transient_states_uk for surface in SURFACES)
    assert all(surface.confirmation_uk for surface in SURFACES)

    disabled = [surface for surface in SURFACES if surface.phase == "visible-disabled"]
    assert {surface.key for surface in disabled} == {
        "opportunities",
        "bookmakers_accounts",
    }
    assert all(surface.target_widget is None and surface.blocked_reason_uk for surface in disabled)
    assert SURFACE_BY_KEY["bookmakers_accounts"].target_widget is None
    settings = SURFACE_BY_KEY["settings"]
    assert settings.phase == "active"
    assert settings.target_widget == "owner_economic_authority_button"
    assert settings.blocked_reason_uk is None


def test_shell_detail_lines_expose_phase_and_truth_boundaries() -> None:
    for surface in SURFACES:
        lines = surface_detail_lines(surface)
        assert lines[0].startswith("СТАН:")
        assert any(line.startswith("Межа істини:") for line in lines)
        assert any(line.startswith("Перезапуск:") for line in lines)
        if surface.phase == "visible-disabled":
            assert any(line.startswith("Чому вимкнено:") for line in lines)


def test_shell_selection_persistence_is_atomic_bounded_and_fail_soft(tmp_path) -> None:
    assert load_surface_selection(tmp_path) == DEFAULT_SURFACE_KEY

    assert save_surface_selection(tmp_path, "market_mirror") is True
    assert load_surface_selection(tmp_path) == "market_mirror"
    persisted = json.loads(
        (tmp_path / "windows-shell-state-v1.json").read_text(encoding="utf-8")
    )
    assert persisted == {"version": 1, "surface_key": "market_mirror"}

    assert save_surface_selection(tmp_path, "not-a-real-surface") is False
    assert load_surface_selection(tmp_path) == "market_mirror"

    (tmp_path / "windows-shell-state-v1.json").write_text("{broken", encoding="utf-8")
    assert load_surface_selection(tmp_path) == DEFAULT_SURFACE_KEY
