from __future__ import annotations

import inspect
from pathlib import Path

from autosport.windows_webview_audit import (
    inspect_keyboard_contract,
    inspect_semantic_shell,
)
from autosport.windows_webview_shell import (
    AutosportWebBridge,
    web_shell_index_path,
)


_ROOT = Path(__file__).resolve().parents[1]


def test_semantic_shell_machine_accessibility_contract_passes_without_claiming_nvda() -> None:
    report = inspect_semantic_shell()

    assert report["status"] == "PASS", report["failures"]
    assert report["shell"] == "pywebview-edgechromium-semantic-html"
    assert report["critical_control_count"] >= 40
    assert report["landmarks"] == ["header", "main", "nav"]
    assert report["positive_tabindex"] == []
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["real_money_execution"] is False


def test_semantic_shell_keyboard_contract_passes_without_claiming_physical_proof() -> None:
    report = inspect_keyboard_contract()

    assert report["status"] == "PASS", report["failures"]
    assert len(report["critical_focusable_controls"]) >= 40
    assert report["positive_tabindex"] == []
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["real_money_execution"] is False


def test_webview_bridge_exposes_only_narrow_command_and_state_surface() -> None:
    public_methods = {
        name
        for name, value in inspect.getmembers(AutosportWebBridge, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public_methods == {"close", "dispatch", "get_state"}
    source = inspect.getsource(AutosportWebBridge)
    assert "AutosportSession" not in source
    assert "ParlayApiTableTennisProvider" not in source
    assert "EconomicGoalStore" not in source


def test_webview_shell_assets_are_packaged_and_edgechromium_is_explicit() -> None:
    index = web_shell_index_path()
    assert index.is_file()
    assert index.with_name("app.js").is_file()
    assert index.with_name("styles.css").is_file()

    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    build = (_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    shell_source = (
        _ROOT / "src" / "autosport" / "windows_webview_shell.py"
    ).read_text(encoding="utf-8")

    assert '"pywebview==6.2.1"' in pyproject
    assert "windows_web/*.html" in pyproject
    assert "--add-data $trustedWebAssetsSpec" in build
    assert 'webview.start(gui="edgechromium")' in shell_source


def test_packaged_interactive_entry_no_longer_imports_legacy_tk_operator_shell() -> None:
    source = (_ROOT / "src" / "autosport" / "windows_entry.py").read_text(
        encoding="utf-8"
    )

    assert "from autosport.windows_webview_shell" in source
    assert "from autosport.windows_gui import main as gui_main" not in source
    assert "install_compact_windows_layout()" not in source


def test_webview_dynamic_projection_preserves_nvda_navigation_contract() -> None:
    index = _ROOT / "src" / "autosport" / "windows_web" / "index.html"
    html = index.read_text(encoding="utf-8")
    javascript = index.with_name("app.js").read_text(encoding="utf-8")

    assert 'event.ctrlKey && event.altKey' not in javascript
    for marker in (
        "event.altKey",
        "event.ctrlKey",
        "event.metaKey",
        "event.shiftKey",
        "if (hasShortcutModifier(event)) return;",
    ):
        assert marker in javascript
    modifier_guard_index = javascript.index(
        "if (hasShortcutModifier(event)) return;"
    )
    assert modifier_guard_index < javascript.index('event.key === "F2"')
    assert modifier_guard_index < javascript.index('event.key === "F8"')
    for marker in (
        'key === "r"',
        'key === "o"',
        'key === "e"',
        'key === "l"',
        'event.key === "F6"',
        'event.key === "F7"',
        'event.key === "F9"',
        'event.key === "F10"',
    ):
        assert marker not in javascript
    assert "replaceChildren()" not in javascript
    assert "setTextIfChanged(statusNode" in javascript
    assert "setTextIfChanged(errorNode" in javascript
    assert "syncTextChildren(node, values" in javascript
    assert 'setTextIfChanged(byId("live-status"), state.live_status || "")' in javascript
    assert 'setTextIfChanged(byId("manual-status"), state.manual.status || "")' in javascript
    assert 'byId("manual-status").textContent = state.manual.status || ""' not in javascript
    assert '<p id="live-status" role="status"' not in html
    assert '<p id="manual-status" role="status" aria-live="polite">' in html

    assert 'dispatch("replay.run", {' in javascript
    assert 'dataset_path: byId("dataset-path").value' in javascript
    assert 'research_plan_path: byId("research-plan-path").value' in javascript
    assert 'dispatch("recovery.run")' in javascript
    assert 'byId(102).addEventListener("click"' in javascript
    assert 'byId(108).addEventListener("click"' in javascript
