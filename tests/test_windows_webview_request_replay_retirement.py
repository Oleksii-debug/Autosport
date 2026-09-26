from __future__ import annotations

import threading

import autosport.windows_webview_emergency_stop as emergency_module
from autosport.windows_webview_emergency_stop import EmergencyStopWebController
from autosport.windows_webview_shell import _REQUEST_REPLAY_LIMIT


class _ReplayController(EmergencyStopWebController):
    """Minimal real dispatcher with a counted mutating action."""

    def __init__(self) -> None:
        # Keep the test on the production request reservation/store implementation
        # without constructing unrelated economic/provider services.
        self._lock = threading.RLock()
        self._request_replay_lock = threading.RLock()
        self._request_identities: dict[str, str] = {}
        self._request_results: dict[str, tuple[str, dict[str, object]]] = {}
        self._retired_request_ids: set[bytes] = set()
        self._request_replay_saturated = False
        self._emergency_dispatch_lock = threading.RLock()
        self._bridge_validation_error = ""
        self._closing = False
        self.mutations = 0

    def _action_manual_clear(self, payload):
        del payload
        self.mutations += 1
        return {"status": "completed", "message": "mutated"}


def _command(request_id: str, *, changed: bool = False) -> dict[str, object]:
    payload = {"changed": True} if changed else {}
    return {
        "request_id": request_id,
        "action_id": "manual.clear",
        "payload": payload,
    }


def test_evicted_request_id_never_executes_mutating_action_again() -> None:
    controller = _ReplayController()

    first = controller.dispatch(_command("original"))
    assert first["status"] == "completed"
    assert controller.mutations == 1

    # Fill beyond the bounded response cache so the first response is evicted.
    for index in range(_REQUEST_REPLAY_LIMIT):
        result = controller.dispatch(_command(f"later-{index}"))
        assert result["status"] == "completed"

    assert "original" not in controller._request_results
    assert "original" not in controller._request_identities
    mutations_after_eviction = controller.mutations

    replay = controller.dispatch(_command("original"))
    collision = controller.dispatch(_command("original", changed=True))

    assert replay["status"] == "rejected"
    assert collision["status"] == "rejected"
    assert controller.mutations == mutations_after_eviction


def test_retirement_capacity_saturates_fail_closed_without_losing_cached_replay(
    monkeypatch,
) -> None:
    monkeypatch.setattr(emergency_module, "_RETIRED_REQUEST_ID_LIMIT", 1)
    controller = _ReplayController()

    assert controller.dispatch(_command("retired-0"))["status"] == "completed"
    for index in range(_REQUEST_REPLAY_LIMIT):
        assert controller.dispatch(_command(f"cached-{index}"))["status"] == "completed"

    assert len(controller._retired_request_ids) == 1
    assert controller._request_replay_saturated is False

    # One more successful new request needs another eviction. With the bounded
    # tombstone budget exhausted, the implementation enters fail-closed mode.
    before_saturation = controller.mutations
    assert controller.dispatch(_command("saturating"))["status"] == "completed"
    assert controller.mutations == before_saturation + 1
    assert controller._request_replay_saturated is True

    # Existing cached results remain replayable without another mutation.
    cached_mutations = controller.mutations
    cached = controller.dispatch(_command("cached-255"))
    assert cached["status"] == "completed"
    assert controller.mutations == cached_mutations

    # A previously unseen request cannot execute after saturation.
    fresh = controller.dispatch(_command("must-not-execute"))
    assert fresh["status"] == "rejected"
    assert controller.mutations == cached_mutations
