from __future__ import annotations

import threading

import pytest

from autosport.windows_webview_shell import AutosportWebController


def _bare_controller() -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller._request_results = {}
    controller._closing = False
    controller.status = "stable product status"
    controller.last_error = ""
    controller.log = []
    controller.manual_result = "prior"
    controller.manual_status = "ready"
    return controller


@pytest.mark.parametrize(
    ("raw", "expected_request_id", "expected_message"),
    [
        (
            "not-a-mapping",
            "invalid",
            "Некоректна команда інтерфейсу.",
        ),
        (
            {
                "request_id": " ",
                "action_id": "manual.clear",
                "payload": {},
            },
            "invalid",
            "Некоректний ідентифікатор команди.",
        ),
        (
            {
                "request_id": "bad-action-shape",
                "action_id": None,
                "payload": {},
            },
            "bad-action-shape",
            "Некоректна команда інтерфейсу.",
        ),
        (
            {
                "request_id": "bad-payload-shape",
                "action_id": "manual.clear",
                "payload": [],
            },
            "bad-payload-shape",
            "Некоректна команда інтерфейсу.",
        ),
        (
            {
                "request_id": "non-json-payload",
                "action_id": "manual.clear",
                "payload": {"value": float("nan")},
            },
            "non-json-payload",
            "Некоректні дані команди інтерфейсу.",
        ),
        (
            {
                "request_id": "unknown-action",
                "action_id": "not.a.real.action",
                "payload": {},
            },
            "unknown-action",
            "Невідома команда інтерфейсу.",
        ),
    ],
)
def test_bridge_validation_rejection_survives_immediate_state_refresh_projection(
    raw: object,
    expected_request_id: str,
    expected_message: str,
) -> None:
    controller = _bare_controller()

    result = controller.dispatch(raw)  # type: ignore[arg-type]

    assert result == {
        "request_id": expected_request_id,
        "status": "rejected",
        "message": expected_message,
    }
    # app.js immediately refreshes state after dispatch. state()["last_error"]
    # projects this same field, so the alert cannot disappear in that same cycle.
    assert controller.last_error == expected_message

    # Bridge validation is presentation state only: no domain status/log event is
    # manufactured merely to keep an accessibility alert discoverable.
    assert controller.status == "stable product status"
    assert controller.log == []


def test_conflicting_request_id_persists_rejection_without_reexecuting_action() -> None:
    controller = _bare_controller()
    command = {
        "request_id": "same-request",
        "action_id": "manual.clear",
        "payload": {},
    }

    first = controller.dispatch(command)
    assert first["status"] == "completed"
    first_log = list(controller.log)
    first_manual_result = controller.manual_result

    collision = controller.dispatch(
        {
            "request_id": "same-request",
            "action_id": "manual.clear",
            "payload": {"changed": True},
        }
    )

    assert collision == {
        "request_id": "same-request",
        "status": "rejected",
        "message": "Повторний ідентифікатор належить іншій команді.",
    }
    assert controller.last_error == collision["message"]

    # The conflicting replay is not a second command execution and does not
    # manufacture a second domain/log event.
    assert controller.log == first_log
    assert controller.manual_result == first_manual_result


def test_exact_request_replay_remains_idempotent_and_does_not_add_log_events() -> None:
    controller = _bare_controller()
    command = {
        "request_id": "exact-replay",
        "action_id": "manual.clear",
        "payload": {},
    }

    first = controller.dispatch(command)
    first_log = list(controller.log)
    second = controller.dispatch(command)

    assert second == first
    assert controller.log == first_log
