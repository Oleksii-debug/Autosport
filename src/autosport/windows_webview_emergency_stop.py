from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from .windows_emergency_stop import WindowsEmergencyStopBridge
from .windows_webview_shell import AutosportWebController, _PRODUCT_SOURCE_FACTORY_ENV


_EMERGENCY_ACTION_ID = "emergency_stop.activate"


def _product_runtime_source_configuration_error() -> str:
    source_factory = os.environ.get(_PRODUCT_SOURCE_FACTORY_ENV)
    if source_factory is None or not source_factory:
        return "AUTOSPORT_PRODUCT_SOURCE_FACTORY не задано."
    if source_factory.strip() != source_factory:
        return "AUTOSPORT_PRODUCT_SOURCE_FACTORY має неоднозначний формат."
    return ""


def _project_product_runtime_configuration(state: dict[str, Any]) -> None:
    """Keep packaged START actionability aligned with its existing precondition.

    The source-factory value can name provider composition and must never become
    operator-visible state.  Only a bounded product-owned reason is projected, and
    an already-running runtime keeps its lifecycle status even if the environment
    changes after START.
    """

    product_runtime = state.get("product_runtime")
    if not isinstance(product_runtime, dict) or product_runtime.get("running") is True:
        return
    configuration_error = _product_runtime_source_configuration_error()
    if not configuration_error:
        return

    product_runtime["can_start"] = False
    current_status = product_runtime.get("status")
    status = current_status if isinstance(current_status, str) else ""
    product_runtime["status"] = (
        f"{status} Запуск недоступний: {configuration_error}"
    ).strip()


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

    def state(self) -> dict[str, Any]:
        state = super().state()
        _project_product_runtime_configuration(state)
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
            previous_identity, previous_result = self._reserve_request_identity(
                request_id,
                command_identity,
            )
            if previous_identity is not None:
                if previous_identity != command_identity:
                    return {
                        "request_id": request_id,
                        "status": "rejected",
                        "message": "Повторний ідентифікатор належить іншій команді.",
                    }
                if previous_result is not None:
                    return previous_result
                return {
                    "request_id": request_id,
                    "status": "rejected",
                    "message": "Команда з цим ідентифікатором уже виконується.",
                }

            # Safety control remains callable while ordinary controller work,
            # polling, economic workers, or close-state presentation are busy.
            # It owns no ordinary UI/domain state and publishes only the canonical
            # durable execution STOP authority.
            try:
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
                self._store_request_result(request_id, command_identity, result)
                return result
            except BaseException:
                self._release_request_identity(request_id, command_identity)
                raise
