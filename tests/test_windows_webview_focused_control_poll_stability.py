from __future__ import annotations

from pathlib import Path


APP = Path(__file__).parents[1] / "src" / "autosport" / "windows_web" / "app.js"


def _source() -> str:
    return APP.read_text(encoding="utf-8")


def test_focused_operator_controls_are_not_overwritten_by_state_poll() -> None:
    source = _source()

    assert "function setValueUnlessFocused(node, value)" in source
    assert "document.activeElement !== node" in source

    guarded_projections = (
        "setValueUnlessFocused(strategy, state.strategy_id || previousStrategy);",
        "setValueUnlessFocused(byId(103), String(state.replay_speed));",
        "setValueUnlessFocused(byId(104), state.live_mode);",
        "setValueUnlessFocused(nav, state.surface_key || priorNav);",
    )
    for projection in guarded_projections:
        assert projection in source

    stale_unconditional_projections = (
        "strategy.value = state.strategy_id || previousStrategy;",
        "byId(103).value = String(state.replay_speed);",
        "byId(104).value = state.live_mode;",
        "nav.value = state.surface_key || priorNav;",
    )
    for projection in stale_unconditional_projections:
        assert projection not in source


def test_poll_and_native_accessibility_contract_remain_intact() -> None:
    source = _source()

    assert "window.setInterval(refreshState, 250)" in source
    assert 'event.key === "F2"' in source
    assert 'event.key === "F8"' in source
    assert 'event.key === "F6"' not in source
    assert 'event.key === "F7"' not in source
    assert 'event.key === "F9"' not in source
    assert 'event.key === "F10"' not in source
    assert 'event.key.toLowerCase() === "r"' not in source
    assert "refreshInFlight" in source
    assert "refreshPending" in source
