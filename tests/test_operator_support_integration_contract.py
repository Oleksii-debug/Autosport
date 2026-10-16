"""Source-level checks for operator reports on the existing trusted WebView UI."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "src/autosport/windows_webview_shell.py"
HTML = ROOT / "src/autosport/windows_web/index.html"
JS = ROOT / "src/autosport/windows_web/app.js"


def test_support_export_is_bound_to_canonical_webview_controller():
    shell = SHELL.read_text(encoding="utf-8")
    assert '"support.export": self._action_support_export,' in shell
    assert "self.support_export_worker = OneShotSupportExportWorker()" in shell
    assert "self._enable_support_trace()" in shell
    assert "def _action_support_export(" in shell
    assert '"product.tick"' in shell
    assert "self._support_record(" in shell
    assert "self._support_trace.close()" in shell
    assert '"agent_overview":' in shell
    assert '"support_export":' in shell
    export_action = shell.split("def _action_support_export(", 1)[1].split(
        "def _action_product_source_configure(", 1
    )[0]
    assert "real_money_execution" not in export_action
    assert "self.support_export_worker.start(" in export_action


def test_keyboard_and_screen_reader_report_controls_are_present():
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    for token in (
        "agent-overview-list", "support-folder",
        "support-export-button", "support-export-status",
    ):
        assert f'id="{token}"' in html
    assert 'role="status" aria-live="polite"' in html
    assert 'dispatch("support.export",' in js
    assert 'byId("support-export-button").addEventListener("click"' in js
    assert 'renderList(byId("agent-overview-list"), state.agent_overview || [])' in js
    assert "state.support_export" in js
