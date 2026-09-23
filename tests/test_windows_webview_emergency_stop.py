from __future__ import annotations

from autosport.execution_stop_authority import ExecutionAuthorityMode, ExecutionStopAuthority
from autosport.windows_emergency_stop import execution_stop_path
from autosport.windows_webview_emergency_stop import EmergencyStopWebController


def _command(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "action_id": "emergency_stop.activate",
        "payload": {},
    }


def test_webview_emergency_stop_initializes_missing_authority_stopped(tmp_path):
    controller = EmergencyStopWebController(tmp_path)

    result = controller.dispatch(_command("stop-missing"))

    assert result["status"] == "completed"
    state = ExecutionStopAuthority(execution_stop_path(tmp_path)).current()
    assert state.mode is ExecutionAuthorityMode.STOPPED
    assert state.revision == 1
    assert "не є доказом зупинки вже запущеного" in result["message"]


def test_webview_emergency_stop_transitions_armed_authority_and_is_request_idempotent(
    tmp_path,
):
    authority = ExecutionStopAuthority(execution_stop_path(tmp_path))
    stopped = authority.initialize_stopped(operator_id="test", reason="initial-safe")
    armed = authority.arm(
        operator_id="test",
        reason="explicit-test-arm",
        confirmation_id="confirmed-for-test",
        expected_revision=stopped.revision,
    )
    assert armed.mode is ExecutionAuthorityMode.ARMED

    controller = EmergencyStopWebController(tmp_path)
    command = _command("stop-armed")
    first = controller.dispatch(command)
    second = controller.dispatch(command)

    assert first == second
    assert first["status"] == "completed"
    current = authority.current()
    assert current.mode is ExecutionAuthorityMode.STOPPED
    assert current.revision == armed.revision + 1

    third = controller.dispatch(_command("stop-again"))
    assert third["status"] == "completed"
    assert authority.current().revision == current.revision


def test_webview_emergency_stop_is_not_gated_by_ordinary_busy_or_closing_state(tmp_path):
    authority = ExecutionStopAuthority(execution_stop_path(tmp_path))
    stopped = authority.initialize_stopped(operator_id="test", reason="initial-safe")
    authority.arm(
        operator_id="test",
        reason="explicit-test-arm",
        confirmation_id="busy-test-confirmation",
        expected_revision=stopped.revision,
    )

    controller = EmergencyStopWebController(tmp_path)
    controller._closing = True

    result = controller.dispatch(_command("stop-while-closing"))

    assert result["status"] == "completed"
    assert authority.current().mode is ExecutionAuthorityMode.STOPPED


def test_webview_emergency_stop_corrupt_authority_fails_closed_without_overwrite(tmp_path):
    path = execution_stop_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = b'{"partial":true}\n'
    path.write_bytes(corrupt)

    controller = EmergencyStopWebController(tmp_path)
    result = controller.dispatch(_command("stop-corrupt"))

    assert result["status"] == "rejected"
    assert "НЕ ПІДТВЕРДЖЕНО" in result["message"]
    assert path.read_bytes() == corrupt
    assert not path.with_name(path.name + ".anchor.json").exists()


def test_webview_emergency_stop_rejects_payload_and_request_id_reuse(tmp_path):
    controller = EmergencyStopWebController(tmp_path)
    ok = controller.dispatch(_command("same-id"))
    assert ok["status"] == "completed"

    payload_result = controller.dispatch(
        {
            "request_id": "payload",
            "action_id": "emergency_stop.activate",
            "payload": {"force": True},
        }
    )
    assert payload_result["status"] == "rejected"

    reused = controller.dispatch(
        {
            "request_id": "same-id",
            "action_id": "emergency_stop.activate",
            "payload": {"force": True},
        }
    )
    assert reused["status"] == "rejected"
