from __future__ import annotations

import threading

from autosport.windows_webview_shell import (
    AutosportWebController,
    _REQUEST_REPLAY_LIMIT,
)


def _bare_controller() -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller._request_results = {}
    controller._closing = False
    controller.manual_result = "initial"
    controller.manual_status = ""
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    return controller


def _command(request_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "request_id": request_id,
        "action_id": "manual.clear",
        "payload": {} if payload is None else payload,
    }


def _apply_replay_pressure(controller: AutosportWebController) -> None:
    for index in range(_REQUEST_REPLAY_LIMIT):
        result = controller.dispatch(_command(f"pressure-{index}"))
        assert result["status"] == "completed"


def test_completed_mutating_request_id_cannot_execute_again_after_cache_pressure() -> None:
    controller = _bare_controller()
    original = _command("original-request")

    first = controller.dispatch(original)
    assert first["status"] == "completed"

    _apply_replay_pressure(controller)
    controller.manual_result = "must-survive-duplicate-replay"

    second = controller.dispatch(original)

    assert second == first
    assert controller.manual_result == "must-survive-duplicate-replay"


def test_evicted_request_id_cannot_be_rebound_to_changed_command_identity() -> None:
    controller = _bare_controller()
    original = _command("stable-request")

    first = controller.dispatch(original)
    assert first["status"] == "completed"

    _apply_replay_pressure(controller)
    controller.manual_result = "must-survive-rebind-attempt"

    rebound = controller.dispatch(
        _command("stable-request", {"changed_after_eviction": True})
    )

    assert rebound["status"] == "rejected"
    assert "іншій команді" in str(rebound["message"])
    assert controller.manual_result == "must-survive-rebind-attempt"
