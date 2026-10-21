"""Plan 6 §3А: keyboard-readable agent identities are bound to real strategy authority.

The readback is an observation of StrategySpec, never a renderer-controlled model
selector, provider capability or economic execution permission.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from autosport.strategies import available_strategies, strategy_spec
from autosport.windows_webview_shell import AutosportWebController


_ROOT = Path(__file__).resolve().parents[1]
_WEB_DIR = _ROOT / "src" / "autosport" / "windows_web"


@pytest.mark.parametrize("spec", available_strategies(), ids=lambda spec: spec.strategy_id)
def test_agent_projection_uses_exact_authoritative_strategy_spec(
    tmp_path: Path, spec,
) -> None:
    controller = AutosportWebController(tmp_path)
    controller.strategy_id = spec.strategy_id

    observed = controller.state()
    canonical = strategy_spec(spec.strategy_id)
    assert observed["strategy_id"] == canonical.strategy_id
    assert observed["strategy_agent_names"] == list(canonical.agent_names)
    assert observed["strategy_agent_composition_sha256"] == canonical.agent_composition_sha256
    assert len(observed["strategy_agent_composition_sha256"]) == 64
    assert observed["truth"]["real_money_execution"] is False
    assert observed["truth"]["nvda_verified"] is False


def test_agent_identity_is_purely_observational_across_restart(tmp_path: Path) -> None:
    first = AutosportWebController(tmp_path)
    second = AutosportWebController(tmp_path)
    for spec in available_strategies():
        first.strategy_id = spec.strategy_id
        second.strategy_id = spec.strategy_id
        projection = first.state()
        restored = second.state()
        assert projection["strategy_agent_names"] == restored["strategy_agent_names"]
        assert projection["strategy_agent_composition_sha256"] == (
            restored["strategy_agent_composition_sha256"]
        )
        assert projection["strategy_agent_composition_sha256"] == (
            spec.agent_composition_sha256
        )


def test_readonly_agent_controls_are_product_integrated_labeled_and_keyboard_accessible() -> None:
    html = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
    for field in ("strategy-agent-names", "strategy-agent-composition-sha"):
        assert html.count(f'id="{field}"') == 1
        assert f'<label for="{field}">' in html
        assert f'<input id="{field}" type="text" readonly' in html
    assert "незалежний перемикач зовнішньої ШІ-моделі" in html
    assert 'id="106"' in html


def test_agent_rendering_is_fail_closed_during_malformed_state_and_strategy_change() -> None:
    js = (_WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "Array.isArray(value.strategy_agent_names)" in js
    assert "new Set(value.strategy_agent_names).size" in js
    assert "!/^[0-9a-f]{64}$/.test(value.strategy_agent_composition_sha256)" in js
    assert 'state.strategy_agent_names.join(", ")' in js
    assert '"strategy-agent-composition-sha"), state.strategy_agent_composition_sha256' in js
    assert 'setValueIfChanged(byId("strategy-agent-names"), "—");' in js
    assert 'setValueIfChanged(byId("strategy-agent-composition-sha"), "—");' in js
    # No JS command may select or recompose the actual backend agents.
    assert "strategy_agent_names:" not in js
    assert "agent_composition_sha256:" not in js
