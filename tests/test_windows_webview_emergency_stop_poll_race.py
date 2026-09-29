from __future__ import annotations

from autosport.windows_webview_shell import web_shell_index_path


def _asset(name: str) -> str:
    return web_shell_index_path().with_name(name).read_text(encoding="utf-8")


def test_emergency_stop_pending_feedback_is_isolated_from_ordinary_state_poll() -> None:
    script = _asset("emergency_stop.js")

    begin = script.index("function beginPendingStatus(message)")
    finish = script.index("function finishPendingStatus(message)", begin)
    activation = script.index("async function activateEmergencyStop()", finish)
    begin_body = script[begin:finish]
    finish_body = script[finish:activation]

    # While the independent safety command is in flight, the ordinary 250 ms state
    # poll may still return a pre-command durable snapshot. That snapshot must not
    # overwrite the focused/assertive pending feedback seen by a blind operator.
    assert 'status.hidden = true;' in begin_body
    assert 'status.setAttribute("aria-live", "off");' in begin_body
    assert 'node.id = "emergency-stop-pending-status";' in begin_body
    assert 'node.setAttribute("role", "status");' in begin_body
    assert 'node.setAttribute("aria-live", "assertive");' in begin_body
    assert 'node.setAttribute("aria-atomic", "true");' in begin_body
    assert "node.focus();" in begin_body

    # Backend completion hands focus back to the canonical durable status projection;
    # only then may subsequent ordinary polls converge the readback again.
    assert "setStatus(message);" in finish_body
    assert 'status.hidden = false;' in finish_body
    assert 'status.setAttribute("aria-live", "assertive");' in finish_body
    assert "focusStatus();" in finish_body
    assert "pendingStatus.remove();" in finish_body


def test_emergency_stop_pending_fence_spans_the_backend_await() -> None:
    script = _asset("emergency_stop.js")

    pending = script.index("beginPendingStatus(")
    awaited = script.index("const result = await dispatch(", pending)
    finished = script.index("finishPendingStatus(resultMessage);", awaited)

    assert pending < awaited < finished
    between = script[pending:finished]
    assert "postRefresh: false" in between
    assert "refreshState" not in between

    # The transient pending node is presentation-only. It does not call the backend,
    # inspect durable execution authority, or claim that STOP has already succeeded.
    begin = script.index("function beginPendingStatus(message)")
    finish = script.index("function finishPendingStatus(message)", begin)
    begin_body = script[begin:finish]
    assert "pywebview" not in begin_body
    assert "execution_blocked" not in begin_body
    assert "STOP ПІДТВЕРДЖЕНО" not in begin_body
