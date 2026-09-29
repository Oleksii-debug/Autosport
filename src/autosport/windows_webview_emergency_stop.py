from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Mapping

from .windows_emergency_stop import WindowsEmergencyStopBridge
from .windows_webview_shell import AutosportWebController, _REQUEST_REPLAY_LIMIT


_EMERGENCY_ACTION_ID = "emergency_stop.activate"
_RETIRED_REQUEST_ID_LIMIT = 65_536
_RETIRED_REQUEST_SENTINEL = "retired-request-id"


def _request_id_digest(request_id: str) -> bytes:
    return hashlib.sha256(request_id.encode("utf-8")).digest()


class EmergencyStopWebController(AutosportWebController):
    """Compose durable emergency execution STOP into the canonical WebView controller.

    Ordinary ``product_runtime.stop`` remains cooperative worker control. This
    adapter adds one distinct command that only publishes/confirms the canonical
    durable execution-admission STOP authority. It deliberately does not claim
    that already-running provider, feed, worker, or settlement work has drained.

    The packaged Windows path also owns the bridge replay-retirement fence. The
    base controller keeps only a bounded result cache; evicted request identifiers
    are retained here as compact SHA-256 tombstones so an old mutating command can
    never become executable again merely because its response aged out of cache.
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
        self._retired_request_ids: set[bytes] = set()
        self._request_replay_saturated = False

    def _reserve_request_identity(
        self,
        request_id: str,
        command_identity: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Reserve a request without ever readmitting an evicted identifier."""

        with self._request_replay_lock:
            previous_identity = self._request_identities.get(request_id)
            previous = self._request_results.get(request_id)
            if previous_identity is not None:
                return (
                    previous_identity,
                    None if previous is None else dict(previous[1]),
                )

            if (
                self._request_replay_saturated
                or _request_id_digest(request_id) in self._retired_request_ids
            ):
                # Command identities are canonical JSON objects and therefore can
                # never equal this sentinel. The inherited dispatcher consequently
                # rejects the request before action dispatch, even for the same old
                # payload whose result has already been evicted.
                return _RETIRED_REQUEST_SENTINEL, None

            self._request_identities[request_id] = command_identity
            return None, None

    def _store_request_result(
        self,
        request_id: str,
        command_identity: str,
        result: Mapping[str, Any],
    ) -> None:
        """Bound replay payload memory while retaining fail-closed spent IDs."""

        with self._request_replay_lock:
            if self._request_identities.get(request_id) != command_identity:
                raise RuntimeError("request identity reservation changed during dispatch")
            self._request_results[request_id] = (command_identity, dict(result))
            while len(self._request_results) > _REQUEST_REPLAY_LIMIT:
                oldest = next(iter(self._request_results))
                if len(self._retired_request_ids) < _RETIRED_REQUEST_ID_LIMIT:
                    self._retired_request_ids.add(_request_id_digest(oldest))
                else:
                    # Never trade exactly-once safety for memory reclamation. Once
                    # the bounded tombstone budget is exhausted, cached requests may
                    # still replay but every previously unseen request fails closed
                    # until the application is restarted with a fresh bridge session.
                    self._request_replay_saturated = True
                del self._request_results[oldest]
                self._request_identities.pop(oldest, None)

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
