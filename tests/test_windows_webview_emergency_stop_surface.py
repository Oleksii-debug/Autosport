from __future__ import annotations

from pathlib import Path

import autosport.windows_webview_audit as webview_audit
from autosport.windows_webview_shell import web_shell_index_path


def _asset(name: str) -> str:
    return web_shell_index_path().with_name(name).read_text(encoding="utf-8")


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1
    return text.replace(old, new, 1)


def _audit_candidate(monkeypatch, tmp_path: Path, html: str):
    source_index = web_shell_index_path()
    candidate_index = tmp_path / "index.html"
    candidate_index.write_text(html, encoding="utf-8")
    (tmp_path / "app.js").write_text(
        source_index.with_name("app.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(webview_audit, "web_shell_index_path", lambda: candidate_index)
    return (
        webview_audit.inspect_semantic_shell(),
        webview_audit.inspect_keyboard_contract(),
    )


def test_packaged_machine_audit_treats_emergency_stop_as_critical_focusable_control():
    semantic = webview_audit.inspect_semantic_shell()
    keyboard = webview_audit.inspect_keyboard_contract()

    assert semantic["status"] == "PASS", semantic["failures"]
    assert keyboard["status"] == "PASS", keyboard["failures"]
    assert "emergency-stop-action" in keyboard["critical_focusable_controls"]


def test_packaged_machine_audit_rejects_missing_emergency_stop(monkeypatch, tmp_path):
    html = web_shell_index_path().read_text(encoding="utf-8")
    html = _replace_once(
        html,
        '      <button id="emergency-stop-action" type="button" aria-describedby="emergency-stop-boundary emergency-stop-status">Активувати аварійний STOP</button>\n',
        "",
    )

    semantic, keyboard = _audit_candidate(monkeypatch, tmp_path, html)

    assert semantic["status"] == "FAIL"
    assert "id=emergency-stop-action: required semantic control missing" in semantic["failures"]
    assert keyboard["status"] == "FAIL"
    assert any(
        "emergency-stop-action" in failure
        and "critical controls missing" in failure
        for failure in keyboard["failures"]
    )


def test_packaged_machine_audit_rejects_disabled_emergency_stop(monkeypatch, tmp_path):
    html = web_shell_index_path().read_text(encoding="utf-8")
    html = _replace_once(
        html,
        'id="emergency-stop-action" type="button" aria-describedby=',
        'id="emergency-stop-action" type="button" disabled aria-describedby=',
    )

    semantic, keyboard = _audit_candidate(monkeypatch, tmp_path, html)

    assert semantic["status"] == "PASS", semantic["failures"]
    assert keyboard["status"] == "FAIL"
    assert any(
        "emergency-stop-action" in failure
        and "critical controls missing" in failure
        for failure in keyboard["failures"]
    )


def test_packaged_machine_audit_rejects_emergency_stop_without_safety_description(
    monkeypatch,
    tmp_path,
):
    html = web_shell_index_path().read_text(encoding="utf-8")
    html = _replace_once(
        html,
        'aria-describedby="emergency-stop-boundary emergency-stop-status"',
        'aria-describedby="emergency-stop-status"',
    )

    semantic, _ = _audit_candidate(monkeypatch, tmp_path, html)

    assert semantic["status"] == "FAIL"
    assert (
        "emergency STOP must describe both durable safety boundary and current status"
        in semantic["failures"]
    )


def test_packaged_machine_audit_rejects_nonassertive_emergency_status(
    monkeypatch,
    tmp_path,
):
    html = web_shell_index_path().read_text(encoding="utf-8")
    html = _replace_once(
        html,
        'id="emergency-stop-status" role="status" aria-live="assertive" aria-atomic="true"',
        'id="emergency-stop-status" role="status" aria-live="polite" aria-atomic="false"',
    )

    semantic, _ = _audit_candidate(monkeypatch, tmp_path, html)

    assert semantic["status"] == "FAIL"
    assert (
        "emergency STOP status must remain role=status, assertive, and atomic"
        in semantic["failures"]
    )


def test_emergency_stop_is_a_distinct_keyboard_reachable_semantic_control():
    html = web_shell_index_path().read_text(encoding="utf-8")

    assert 'id="emergency-stop-action"' in html
    assert '<button id="emergency-stop-action" type="button"' in html
    assert 'id="emergency-stop-status"' in html
    assert 'aria-live="assertive"' in html
    assert 'src="emergency_stop.js"' in html
    assert "Tab + Enter/Space" in html
    assert 'id="product-runtime-stop"' in html
    assert "стійкий журнал заборони" in html
    assert "не доводить" in html
    assert "durable execution authority" not in html
    assert "worker або feed" not in html


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


def test_main_poll_projects_durable_emergency_stop_state_without_stealing_focus():
    script = _asset("app.js")

    assert "function projectEmergencyStopState(emergency)" in script
    assert 'byId("emergency-stop-status")' in script
    assert "projectEmergencyStopState(state.emergency_stop);" in script
    assert '"Аварійний STOP: стан не підтверджено."' in script

    projection = script.split(
        "function projectEmergencyStopState(emergency)", 1
    )[1].split("function syncTextChildren", 1)[0]
    assert "setTextIfChanged(node, value);" in projection
    assert ".focus(" not in projection
    assert "dispatch(" not in projection
