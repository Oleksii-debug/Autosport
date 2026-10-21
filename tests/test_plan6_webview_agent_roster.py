"""Plan 6 3А: canonical WebView2 agent roster is read-only and truthfully scoped."""
from pathlib import Path

from autosport.windows_webview_shell import AutosportWebController
from autosport.strategies import strategy_spec

WEB_ROOT = Path(__file__).resolve().parents[1] / "src" / "autosport" / "windows_web"


def test_registry_roster_visible_without_learning_or_financial_claim(tmp_path):
    controller = AutosportWebController(tmp_path)
    try:
        state = controller.state()
        roster = state["agent_overview"]
        registered = strategy_spec(state["strategy_id"])
        assert len(roster) == 5
        assert registered.strategy_id in roster[0]
        assert registered.agent_composition_sha256 in roster[2]
        for name in registered.agent_names:
            assert name in roster[1]
        assert "не доводить" in roster[4]
        assert state["truth"]["real_money_execution"] is False
    finally:
        controller.close()


def test_html_and_js_bind_to_existing_roster_without_untrusted_html():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert page.count('id="agent-overview-list"') == 1
    assert 'aria-labelledby="agent-overview-heading"' in page
    assert 'renderList(byId("agent-overview-list"), state.agent_overview || [])' in script
    assert 'value.agent_overview.length !== 5' in script
