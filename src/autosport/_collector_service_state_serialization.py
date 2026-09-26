from __future__ import annotations

"""Serialize canonical collector state and make durable STOP a quiescence boundary.

The durable headless collector state is a read/modify/replace journal. Atomic file
replacement prevents torn bytes but, by itself, does not serialize two independent
state objects (or processes) that read the same predecessor. Reuse the repository's
hardened WorkspaceEconomicLock pathname/handle validation and crash-release semantics,
changing only acquisition to blocking for these collector-control transactions.

A second path-shared lock spans the complete run_cycle mutation window. External STOP
uses that same lock, so STOP is published only after an in-flight catalog/delta/terminal
transaction has quiesced. Signal/internal STOP raised by the cycle itself bypasses only
the recursive acquisition in that same thread and therefore can still abort promptly.
This preserves the single existing collector state/lifecycle/delta authorities while
making "durably STOPPED" mean that no older cycle can publish later data or status.
"""

import os
import threading
from typing import BinaryIO

from . import collector_service as _collector_service
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


_State = _collector_service._CollectorServiceState
_Service = _collector_service.HeadlessCollectorService
_ORIGINAL_UPDATE_ANCHOR = "_collector_service_state_original_update_v1"
_SERIALIZED_UPDATE_ANCHOR = "_collector_service_state_serialized_update_v1"
_ORIGINAL_RUN_CYCLE_ANCHOR = "_collector_service_original_run_cycle_v1"
_SERIALIZED_RUN_CYCLE_ANCHOR = "_collector_service_serialized_run_cycle_v1"
_ORIGINAL_STOP_ANCHOR = "_collector_service_original_stop_v1"
_SERIALIZED_STOP_ANCHOR = "_collector_service_serialized_stop_v1"
_STOP_FIELDS = frozenset({"stopped_at", "stop_reason"})
_CYCLE_DEPTH = threading.local()


def _load_anchor(name: str, current):
    existing = getattr(_collector_service, name, None)
    if existing is None:
        existing = current
        setattr(_collector_service, name, existing)
    if not callable(existing):
        raise RuntimeError(f"collector service authority anchor is invalid: {name}")
    return existing


_ORIGINAL_UPDATE = _load_anchor(_ORIGINAL_UPDATE_ANCHOR, _State._update)
_ORIGINAL_RUN_CYCLE = _load_anchor(_ORIGINAL_RUN_CYCLE_ANCHOR, _Service.run_cycle)
_ORIGINAL_STOP = _load_anchor(_ORIGINAL_STOP_ANCHOR, _Service.stop)


class _CollectorServiceStateMutationLock(WorkspaceEconomicLock):
    """Blocking cross-process lock for one collector-state workspace."""

    FILE_NAME = ".collector-service-state.lock"

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise WorkspaceEconomicLockError(
                    "cannot acquire blocking collector-service state lock"
                ) from exc
            return

        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "cannot acquire blocking collector-service state lock"
            ) from exc


class _CollectorServiceCycleMutationLock(_CollectorServiceStateMutationLock):
    """Serialize an entire collector cycle against external durable STOP."""

    FILE_NAME = ".collector-service-cycle.lock"


def _fence_post_stop_mutation(mutate):
    """Allow explicit resume/STOP control only after durable STOP already exists."""

    def guarded(raw) -> None:
        stopped_before = raw.get("stopped_at") is not None
        before = dict(raw) if stopped_before else None
        mutate(raw)
        if not stopped_before:
            return

        # Explicit resume is the only supported transition that clears STOP.
        if raw.get("stopped_at") is None and raw.get("stop_reason") is None:
            return

        assert before is not None
        changed_non_stop = any(
            raw.get(name) != value
            for name, value in before.items()
            if name not in _STOP_FIELDS
        )
        if changed_non_stop:
            raise _collector_service.CollectorServiceStoppedError(
                "collector run is durably STOPPED; terminal mutation is forbidden until explicit resume"
            )

    return guarded


def _serialized_update(self, mutate) -> None:
    try:
        with _CollectorServiceStateMutationLock(self.path.parent):
            _ORIGINAL_UPDATE(self, _fence_post_stop_mutation(mutate))
    except WorkspaceEconomicLockError as exc:
        raise _collector_service.CollectorServiceError(
            "cannot serialize collector service durable state mutation"
        ) from exc


def _depths() -> dict[int, int]:
    depths = getattr(_CYCLE_DEPTH, "values", None)
    if depths is None:
        depths = {}
        _CYCLE_DEPTH.values = depths
    return depths


def _serialized_run_cycle(self):
    identity = id(self)
    depths = _depths()
    depth = depths.get(identity, 0)
    if depth:
        depths[identity] = depth + 1
        try:
            return _ORIGINAL_RUN_CYCLE(self)
        finally:
            if depths[identity] == 1:
                depths.pop(identity, None)
            else:
                depths[identity] -= 1

    try:
        with _CollectorServiceCycleMutationLock(self._state.path.parent):
            depths[identity] = 1
            try:
                return _ORIGINAL_RUN_CYCLE(self)
            finally:
                depths.pop(identity, None)
    except WorkspaceEconomicLockError as exc:
        raise _collector_service.CollectorServiceError(
            "cannot serialize collector cycle mutation window"
        ) from exc


def _serialized_stop(self, reason: str = "operator_stop") -> None:
    # `_stop_if_requested()` is invoked from inside run_cycle. Reacquiring the same
    # OS lock there would deadlock; that same-thread call already owns the cycle
    # mutation window and immediately raises _StopRequested after publishing STOP.
    if _depths().get(id(self), 0):
        _ORIGINAL_STOP(self, reason)
        return
    try:
        with _CollectorServiceCycleMutationLock(self._state.path.parent):
            _ORIGINAL_STOP(self, reason)
    except WorkspaceEconomicLockError as exc:
        raise _collector_service.CollectorServiceError(
            "cannot serialize collector STOP with in-flight cycle"
        ) from exc


def _install() -> None:
    installed_update = getattr(_collector_service, _SERIALIZED_UPDATE_ANCHOR, None)
    if installed_update is None:
        installed_update = _serialized_update
        setattr(_collector_service, _SERIALIZED_UPDATE_ANCHOR, installed_update)
    if not callable(installed_update):
        raise RuntimeError("collector service state serialization anchor is invalid")

    installed_run_cycle = getattr(_collector_service, _SERIALIZED_RUN_CYCLE_ANCHOR, None)
    if installed_run_cycle is None:
        installed_run_cycle = _serialized_run_cycle
        setattr(_collector_service, _SERIALIZED_RUN_CYCLE_ANCHOR, installed_run_cycle)
    if not callable(installed_run_cycle):
        raise RuntimeError("collector service run-cycle serialization anchor is invalid")

    installed_stop = getattr(_collector_service, _SERIALIZED_STOP_ANCHOR, None)
    if installed_stop is None:
        installed_stop = _serialized_stop
        setattr(_collector_service, _SERIALIZED_STOP_ANCHOR, installed_stop)
    if not callable(installed_stop):
        raise RuntimeError("collector service STOP serialization anchor is invalid")

    _State._update = installed_update
    _Service.run_cycle = installed_run_cycle
    _Service.stop = installed_stop


_install()

__all__: list[str] = []
