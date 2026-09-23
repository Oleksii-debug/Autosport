from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping

from .windows_emergency_stop import WindowsEmergencyStopBridge
from .windows_webview_shell import AutosportWebController


_EMERGENCY_ACTION_ID = "emergency_stop.activate"
_EMERGENCY_REQUEST_REPLAY_LIMIT = 256


class EmergencyStopWebController(AutosportWebController):
    """Compose durable emergency execution STOP into the canonical WebView controller.

    Ordinary ``product_runtime.stop`` remains cooperative worker control. This
    adapter adds one distinct command that only publishes/confirms the canonical
    durable execution-admission STOP authority. It deliberately does not claim
    that already-running provider, feed, worker, or settlement work has drained.
    """

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        emergency_stop: WindowsEmergencyStopBridge | None = None,
    ) -> None:
        super().__init__(workspace)
        self._emergency_stop = emergency_stop or WindowsEmergencyStopBridge.for_workspace(
            self.workspace
        )
        # Emergency STOP is a safety lane, not an ordinary controller command.
        # Its own lock/cache preserve duplicate safety without waiting for
        # AutosportWebController._lock, which may be held by a slow command.
        self._emergency_dispatch_lock = threading.RLock()
        self._emergency_request_results: dict[
            str, tuple[str, dict[str, Any]]
        ] = {}

    def state(self) -> dict[str, Any]:
        state = super().state()
        stop_status = self._emergency_stop.status()
        state["emergency_stop"] = {
            "status": stop_status.message_uk,
            "available": stop_status.available,
            "execution_blocked": stop_status.execution_blocked,
            "mode": stop_status.mode,
            "revision": stop_status.revision,
            "integrity_confirmed": stop_status.integrity_confirmed,
            "initialized": stop_status.initialized,
        }
        return state

    def _activate_emergency_stop(self) -> dict[str, Any]:
        result = self._emergency_stop.activate()
        if not result.stopped:
            return {"status": "rejected", "message": result.message_uk}
        return {
            "status": "completed",
            "message": result.message_uk,
            "focus_id": "emergency-stop-status",
        }

    def dispatch(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or raw.get("action_id") != _EMERGENCY_ACTION_ID:
            return super().dispatch(raw)

        request_id = raw.get("request_id")
        payload = raw.get("payload", {})
        if (
            type(request_id) is not str
            or not request_id
            or request_id.strip() != request_id
            or len(request_id) > 128
            or not isinstance(payload, Mapping)
        ):
            return self._reject_bridge_command(
                "invalid",
                "Некоректна команда аварійного STOP.",
            )
        if dict(payload):
            return self._reject_bridge_command(
                request_id,
                "Аварійний STOP не приймає додаткових параметрів.",
            )

        command_identity = json.dumps(
            {"action_id": _EMERGENCY_ACTION_ID, "payload": {}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._emergency_dispatch_lock:
            previous = self._emergency_request_results.get(request_id)
            if previous is not None:
                previous_identity, previous_result = previous
                if previous_identity != command_identity:
                    return {
                        "request_id": request_id,
                        "status": "rejected",
                        "message": "Повторний ідентифікатор належить іншій команді.",
                    }
                return dict(previous_result)

            # Safety control remains callable while ordinary controller work,
            # polling, economic workers, or close-state presentation are busy.
            # It owns no ordinary UI/domain state and publishes only the canonical
            # durable execution STOP authority.
            try:
                response = self._activate_emergency_stop()
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                response = {
                    "status": "rejected",
                    "message": (
                        "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
                        "Нові виконання мають залишатися заблокованими; перевірте журнал STOP."
                    ),
                }

            result = {"request_id": request_id, **response}
            self._emergency_request_results[request_id] = (
                command_identity,
                dict(result),
            )
            while len(self._emergency_request_results) > _EMERGENCY_REQUEST_REPLAY_LIMIT:
                oldest = next(iter(self._emergency_request_results))
                del self._emergency_request_results[oldest]
            return result
