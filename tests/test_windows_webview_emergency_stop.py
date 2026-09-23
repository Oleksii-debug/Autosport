from __future__ import annotations

import threading

from autosport.execution_stop_authority import ExecutionAuthorityMode, ExecutionStopAuthority
from autosport.windows_emergency_stop import execution_stop_path
from autosport.windows_webview_emergency_stop import EmergencyStopWebController
from autosport.windows_webview_shell import AutosportWebBridge


def _command(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "action_id": "emergency_stop.activate",
        "payload": {},
    }


def test_webview_emergency_stop_missing_state_projects_fail_closed_without_mutation(
    tmp_path,
):
    path = execution_stop_path(tmp_path)
    controller = EmergencyStopWebController(tmp_path)

    state = controller.state()["emergency_stop"]

    assert state["available"] is True
    assert state["execution_blocked"] is True
    assert state["mode"] is None
    assert state["revision"] is None
    assert state["integrity_confirmed"] is False
    assert state["initialized"] is False
    assert "ще не ініціалізовано" in state["status"]
    assert not path.exists()
    assert not path.with_name(path.name + ".anchor.json").exists()


def test_webview_emergency_stop_reopen_projects_durable_stopped_revision(tmp_path):
    authority = ExecutionStopAuthority(execution_stop_path(tmp_path))
    stopped = authority.initialize_stopped(operator_id="test", reason="restart-safe")

    first = EmergencyStopWebController(tmp_path).state()["emergency_stop"]
    reopened = EmergencyStopWebController(tmp_path).state()["emergency_stop"]

    for state in (first, reopened):
        assert state["available"] is True
        assert state["execution_blocked"] is True
        assert state["mode"] == ExecutionAuthorityMode.STOPPED.value
        assert state["revision"] == stopped.revision
        assert state["integrity_confirmed"] is True
        assert state["initialized"] is True
        assert "Аварійний STOP активний" in state["status"]
        assert f"ревізії {stopped.revision}" in state["status"]


def test_webview_emergency_stop_state_refreshes_external_armed_transition(tmp_path):
    authority = ExecutionStopAuthority(execution_stop_path(tmp_path))
    stopped = authority.initialize_stopped(operator_id="test", reason="initial-safe")
    controller = EmergencyStopWebController(tmp_path)

    before = controller.state()["emergency_stop"]
    armed = authority.arm(
        operator_id="test",
        reason="explicit-test-arm",
        confirmation_id="state-refresh-confirmation",
        expected_revision=stopped.revision,
    )
    after = controller.state()["emergency_stop"]

    assert before["mode"] == ExecutionAuthorityMode.STOPPED.value
    assert before["execution_blocked"] is True
    assert after["mode"] == ExecutionAuthorityMode.ARMED.value
    assert after["revision"] == armed.revision
    assert after["execution_blocked"] is False
    assert after["integrity_confirmed"] is True
    assert "допуск виконання дозволено" in after["status"]
    assert "Кнопка аварійного STOP доступна" in after["status"]


def test_webview_emergency_stop_corrupt_state_projection_is_fail_closed_and_read_only(
    tmp_path,
):
    path = execution_stop_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = b'{"partial":true}\n'
    path.write_bytes(corrupt)
    controller = EmergencyStopWebController(tmp_path)

    state = controller.state()["emergency_stop"]

    assert state["available"] is True
    assert state["execution_blocked"] is True
    assert state["mode"] is None
    assert state["revision"] is None
    assert state["integrity_confirmed"] is False
    assert state["initialized"] is True
    assert "не підтверджено" in state["status"]
    assert path.read_bytes() == corrupt
    assert not path.with_name(path.name + ".anchor.json").exists()


def test_webview_emergency_stop_initializes_missing_authority_stopped(tmp_path):
    controller = EmergencyStopWebController(tmp_path)

    result = controller.dispatch(_command("stop-missing"))

    assert result["status"] == "completed"
    state = ExecutionStopAuthority(execution_stop_path(tmp_path)).current()
    assert state.mode is ExecutionAuthorityMode.STOPPED
    assert state.revision == 1
    assert "не доводить завершення вже запущеного" in result["message"]

    projected = controller.state()["emergency_stop"]
    assert projected["mode"] == ExecutionAuthorityMode.STOPPED.value
    assert projected["revision"] == 1
    assert projected["execution_blocked"] is True
    assert projected["integrity_confirmed"] is True


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

    ordinary_first = controller.dispatch(
        {
            "request_id": "ordinary-first",
            "action_id": "manual.clear",
            "payload": {},
        }
    )
    assert ordinary_first["status"] == "completed"
    emergency_reuse = controller.dispatch(_command("ordinary-first"))
    assert emergency_reuse["status"] == "rejected"

    emergency_first = controller.dispatch(_command("emergency-first"))
    assert emergency_first["status"] == "completed"
    ordinary_reuse = controller.dispatch(
        {
            "request_id": "emergency-first",
            "action_id": "manual.clear",
            "payload": {},
        }
    )
    assert ordinary_reuse["status"] == "rejected"



class _TrustedWindow:
    def __init__(self) -> None:
        self.current_url = "http://127.0.0.1:41000/index.html"

    def get_current_url(self) -> str:
        return self.current_url


def test_webview_emergency_stop_bypasses_blocked_ordinary_bridge_dispatch(tmp_path):
    authority = ExecutionStopAuthority(execution_stop_path(tmp_path))
    stopped = authority.initialize_stopped(operator_id="test", reason="initial-safe")
    authority.arm(
        operator_id="test",
        reason="explicit-test-arm",
        confirmation_id="blocked-dispatch-confirmation",
        expected_revision=stopped.revision,
    )

    controller = EmergencyStopWebController(tmp_path)
    ordinary_entered = threading.Event()
    release_ordinary = threading.Event()
    emergency_done = threading.Event()
    ordinary_results: list[dict[str, object]] = []
    emergency_results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def blocked_manual_clear(_payload):
        ordinary_entered.set()
        if not release_ordinary.wait(5):
            raise RuntimeError("test did not release blocked ordinary dispatch")
        return {"status": "completed", "message": "ordinary released"}

    controller._action_manual_clear = blocked_manual_clear  # type: ignore[method-assign]
    bridge = AutosportWebBridge(controller)
    bridge._bind_trusted_window(_TrustedWindow())

    def run_ordinary() -> None:
        try:
            ordinary_results.append(
                bridge.dispatch(
                    {
                        "request_id": "ordinary-blocked",
                        "action_id": "manual.clear",
                        "payload": {},
                    }
                )
            )
        except BaseException as exc:
            errors.append(exc)

    def run_emergency() -> None:
        try:
            emergency_results.append(bridge.dispatch(_command("urgent-stop")))
        except BaseException as exc:
            errors.append(exc)
        finally:
            emergency_done.set()

    ordinary_thread = threading.Thread(target=run_ordinary)
    emergency_thread = threading.Thread(target=run_emergency)
    ordinary_thread.start()
    emergency_started = False
    try:
        assert ordinary_entered.wait(5)
        emergency_thread.start()
        emergency_started = True

        # The safety command must finish while the ordinary handler still owns
        # the normal controller lane; releasing that handler happens only below.
        assert emergency_done.wait(5)
        assert errors == []
        assert emergency_results[0]["status"] == "completed"
        assert authority.current().mode is ExecutionAuthorityMode.STOPPED
        assert ordinary_thread.is_alive()
    finally:
        release_ordinary.set()
        ordinary_thread.join(5)
        if emergency_started:
            emergency_thread.join(5)

    assert not ordinary_thread.is_alive()
    assert not emergency_thread.is_alive()
    assert errors == []
    assert ordinary_results[0]["status"] == "completed"
