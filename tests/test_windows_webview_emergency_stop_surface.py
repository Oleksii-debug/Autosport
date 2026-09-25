from __future__ import annotations

from pathlib import Path

import autosport.windows_webview_audit as webview_audit
from autosport.windows_webview_shell import web_shell_index_path


def _asset(name: str) -> str:
    return web_shell_index_path().with_name(name).read_text(encoding="utf-8")


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1
    return text.replace(old, new, 1)


def _audit_candidate(
    monkeypatch,
    tmp_path: Path,
    html: str,
    *,
    emergency_script: str | None = None,
):
    source_index = web_shell_index_path()
    candidate_index = tmp_path / "index.html"
    candidate_index.write_text(html, encoding="utf-8")
    (tmp_path / "app.js").write_text(
        source_index.with_name("app.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "emergency_stop.js").write_text(
        (
            source_index.with_name("emergency_stop.js").read_text(encoding="utf-8")
            if emergency_script is None
            else emergency_script
        ),
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


def test_packaged_keyboard_audit_rejects_unloaded_emergency_stop_script(
    monkeypatch,
    tmp_path,
):
    html = web_shell_index_path().read_text(encoding="utf-8")
    html = _replace_once(
        html,
        '  <script src="emergency_stop.js"></script>\n',
        "",
    )

    _, keyboard = _audit_candidate(monkeypatch, tmp_path, html)

    assert keyboard["status"] == "FAIL"
    assert (
        "emergency STOP keyboard asset is not loaded by the semantic shell"
        in keyboard["failures"]
    )


def test_packaged_keyboard_audit_rejects_broken_emergency_stop_click_wiring(
    monkeypatch,
    tmp_path,
):
    html = web_shell_index_path().read_text(encoding="utf-8")
    emergency_script = _asset("emergency_stop.js")
    emergency_script = _replace_once(
        emergency_script,
        'button.addEventListener("click", activateEmergencyStop);',
        'button.addEventListener("pointerdown", activateEmergencyStop);',
    )

    _, keyboard = _audit_candidate(
        monkeypatch,
        tmp_path,
        html,
        emergency_script=emergency_script,
    )

    assert keyboard["status"] == "FAIL"
    assert any(
        "emergency STOP keyboard wiring marker missing" in failure
        and 'button.addEventListener("click", activateEmergencyStop);' in failure
        for failure in keyboard["failures"]
    )


def test_packaged_machine_audit_rejects_missing_emergency_stop(monkeypatch, tmp_path):
    html = web_shell_index_path().read_text(encoding="utf-8")
    html = _replace_once(
        html,
        (
            '      <button id="emergency-stop-action" type="button" '
            'aria-describedby="emergency-stop-boundary emergency-stop-status">'
            "Активувати аварійний STOP</button>\n"
        ),
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

    assert semantic["status"] == "FAIL"
    assert (
        "emergency STOP must remain enabled in static shell semantics"
        in semantic["failures"]
    )
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


def test_emergency_stop_frontend_reuses_shared_ordered_dispatch():
    app_script = _asset("app.js")
    script = _asset("emergency_stop.js")

    assert "globalThis.autosportDispatch = dispatch;" in app_script
    assert "const dispatch = globalThis.autosportDispatch;" in script
    assert '"emergency_stop.activate",' in script
    assert "{ globalAnnouncement: false, resultFocus: false }" in script
    assert 'getElementById("emergency-stop-action")' in script
    assert 'getElementById("emergency-stop-status")' in script
    assert "globalThis.pywebview.api.dispatch" not in script
    assert "globalThis.pywebview.api.get_state" not in script
    assert "readEmergencyState" not in script
    assert "product_runtime.stop" not in script
    assert "disabled =" not in script
    assert "Ctrl+Shift+S" not in script


def test_emergency_stop_announces_truthful_pending_state_before_ordered_refresh():
    script = _asset("emergency_stop.js")

    pending = script.index("Аварійний STOP: запит передано.")
    awaited_dispatch = script.index("const result = await dispatch(")
    assert pending < awaited_dispatch
    assert "Очікується підтвердження стійкого журналу заборони." in script
    assert script[:awaited_dispatch].count("focusStatus();") >= 1
    # Pending feedback must not falsely claim that durable STOP is already proven.
    pre_dispatch = script[:awaited_dispatch]
    assert "STOP ПІДТВЕРДЖЕНО" not in pre_dispatch
    assert "execution_blocked" not in pre_dispatch


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


def test_emergency_stop_uses_one_live_region_announcement_authority():
    app_script = _asset("app.js")
    emergency_script = _asset("emergency_stop.js")

    assert "async function dispatch(actionId, payload = {}, options = {})" in app_script
    assert "const useGlobalAnnouncement = options.globalAnnouncement !== false;" in app_script
    assert "const useResultFocus = options.resultFocus !== false;" in app_script
    assert "if (useResultFocus) focusResult(result);" in app_script
    assert app_script.count("if (useGlobalAnnouncement) {") == 2

    assert '"emergency_stop.activate",' in emergency_script
    assert "{ globalAnnouncement: false, resultFocus: false }" in emergency_script
    assert 'result.status !== "completed"' in emergency_script
    assert "result.message" in emergency_script
    assert 'getElementById("emergency-stop-status")' in emergency_script

    # Emergency outcome text belongs to the dedicated assertive STOP status.
    # The emergency asset must not write either global action live region.
    assert "app-status" not in emergency_script
    assert "error-status" not in emergency_script
    assert "globalThis.pywebview.api.dispatch" not in emergency_script


def test_emergency_stop_local_failure_keeps_dedicated_accessible_readback():
    script = _asset("emergency_stop.js")

    assert 'typeof dispatch !== "function"' in script
    assert script.count("setStatus(") >= 5
    assert script.count("focusStatus();") >= 3
    assert "Канал застосунку недоступний." in script
    assert "перевірте журнал STOP." in script
