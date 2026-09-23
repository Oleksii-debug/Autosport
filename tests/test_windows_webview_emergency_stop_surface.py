from __future__ import annotations

from pathlib import Path

from autosport.windows_webview_shell import web_shell_index_path


def _asset(name: str) -> str:
    return web_shell_index_path().with_name(name).read_text(encoding="utf-8")


def test_emergency_stop_is_a_distinct_keyboard_reachable_semantic_control():
    html = web_shell_index_path().read_text(encoding="utf-8")

    assert 'id="emergency-stop-action"' in html
    assert '<button id="emergency-stop-action" type="button"' in html
    assert 'id="emergency-stop-status"' in html
    assert 'aria-live="assertive"' in html
    assert 'src="emergency_stop.js"' in html
    assert "Tab + Enter/Space" in html
    assert 'id="product-runtime-stop"' in html
    assert "durable execution authority" in html
    assert "не доводить" in html


def test_emergency_stop_frontend_dispatches_only_the_dedicated_authority_command():
    script = _asset("emergency_stop.js")

    assert 'action_id: "emergency_stop.activate"' in script
    assert "payload: {}" in script
    assert 'getElementById("emergency-stop-action")' in script
    assert 'getElementById("emergency-stop-status")' in script
    assert "product_runtime.stop" not in script
    assert "disabled =" not in script
    assert "Ctrl+Shift+S" not in script


def test_package_data_policy_includes_the_emergency_stop_script():
    project_root = Path(__file__).resolve().parents[1]
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")

    assert '"windows_web/*.js"' in pyproject


def test_packaged_interactive_entry_uses_emergency_stop_controller():
    import autosport.windows_entry as entry

    source = Path(entry.__file__).read_text(encoding="utf-8")
    assert "EmergencyStopWebController" in source
    assert "AutosportWebBridge(controller)" in source
    assert "launch_windows_shell" in source
