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

    # Backend completion restores the canonical durable status projection; the
    # activation path performs the single explicit focus handoff after restoration.
    assert "setStatus(message);" in finish_body
    assert 'status.hidden = false;' in finish_body
    assert 'status.setAttribute("aria-live", "assertive");' in finish_body
    assert "pendingStatus.remove();" in finish_body
    assert "focusStatus();" not in finish_body


def test_emergency_stop_pending_fence_spans_the_backend_await() -> None:
    script = _asset("emergency_stop.js")

    activation = script.index("async function activateEmergencyStop()")
    pending = script.index("beginPendingStatus(", activation)
    awaited = script.index("const result = await dispatch(", pending)
    finished = script.index("finishPendingStatus(resultMessage);", awaited)
    focused = script.index("focusStatus();", finished)

    assert pending < awaited < finished < focused
    between = script[pending:finished]
    assert "postRefresh: false" in between
    assert "refreshState" not in between

    # The transient pending node is presentation-only. It does not call the backend,
    # inspect durable execution authority, or claim that STOP has already succeeded.
    begin = script.index("function beginPendingStatus(message)")
    finish = script.index("function focusPendingStatus()", begin)
    begin_body = script[begin:finish]
    assert "pywebview" not in begin_body
    assert "execution_blocked" not in begin_body
    assert "STOP ПІДТВЕРДЖЕНО" not in begin_body


def test_repeated_emergency_stop_activation_reuses_one_inflight_command() -> None:
    script = _asset("emergency_stop.js")

    activation = script.index("async function activateEmergencyStop()")
    body = script[activation:]
    guard = body.index("if (activationInFlight)")
    begin = body.index("beginPendingStatus(", guard)
    mark_inflight = body.index("activationInFlight = true;", begin)
    awaited = body.index("const result = await dispatch(", mark_inflight)
    clear_inflight = body.index("activationInFlight = false;", awaited)
    finished = body.index("finishPendingStatus(resultMessage);", clear_inflight)
    focused = body.index("focusStatus();", finished)

    assert guard < begin < mark_inflight < awaited < clear_inflight < finished < focused
    guarded = body[guard:begin]
    assert "focusPendingStatus();" in guarded
    assert "return;" in guarded

    # The emergency button remains statically enabled; frontend coalescing is an
    # ordering fence only, not a new permission gate or a disabled safety control.
    assert "button.disabled" not in script
    assert "disabled =" not in script


def test_uncertain_emergency_stop_copy_never_promises_execution_block() -> None:
    """Null/exceptional command results cannot certify a durable STOP journal."""
    script = _asset("emergency_stop.js")
    assert script.count(
        "Не вважайте нові виконання заблокованими без підтвердження"
    ) == 2
    null_outcome = script.split("if (result === null) {", 1)[1].split(
        "} else {", 1
    )[0]
    exception_outcome = script.split("} catch (_error) {", 1)[1].split(
        "} finally {", 1
    )[0]
    for outcome in (null_outcome, exception_outcome):
        assert "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО" in outcome
        assert "Не вважайте нові виконання заблокованими без підтвердження" in outcome
        assert "Нові виконання мають залишатися заблокованими" not in outcome
        assert "перевірте стійкий журнал STOP" in outcome


def test_uncertain_stop_copy_regression_falsifier() -> None:
    """A former unsafe success implication must fail the safety-copy predicate."""
    source = _asset("emergency_stop.js")
    unsafe = source.replace(
        "Не вважайте нові виконання заблокованими без підтвердження",
        "Нові виконання мають залишатися заблокованими",
        1,
    )
    null_outcome = unsafe.split("if (result === null) {", 1)[1].split(
        "} else {", 1
    )[0]
    assert "Нові виконання мають залишатися заблокованими" in null_outcome
    assert "Нові виконання мають залишатися заблокованими" not in source
