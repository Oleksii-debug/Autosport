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

The installed wrappers are built from closure-owned canonical dependencies. Module
names remain inspectable for diagnostics, but rebinding those names after installation
cannot retarget the already-installed state/cycle/STOP authority.
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


def _make_post_stop_fence(*, stopped_error_type, stop_fields):
    frozen_stop_fields = frozenset(stop_fields)
    dict_type = dict

    def fence(mutate):
        def guarded(raw) -> None:
            stopped_before = raw.get("stopped_at") is not None
            before = dict_type(raw) if stopped_before else None
            mutate(raw)
            if not stopped_before:
                return

            assert before is not None
            before_non_stop = {
                name: value
                for name, value in before.items()
                if name not in frozen_stop_fields
            }
            after_non_stop = {
                name: value
                for name, value in raw.items()
                if name not in frozen_stop_fields
            }
            if after_non_stop != before_non_stop:
                raise stopped_error_type(
                    "collector run is durably STOPPED; terminal mutation is forbidden until explicit resume"
                )

            stopped_after = raw.get("stopped_at") is not None
            reason_after = raw.get("stop_reason") is not None
            if stopped_after != reason_after:
                raise stopped_error_type(
                    "collector run STOP state cannot become incomplete during mutation"
                )

            # Explicit resume may clear only the STOP pair. Exact non-STOP mapping
            # equality above also rejects added/deleted fields before durable publish.
            if not stopped_after:
                return

        return guarded

    return fence


_fence_post_stop_mutation = _make_post_stop_fence(
    stopped_error_type=_collector_service.CollectorServiceStoppedError,
    stop_fields=_STOP_FIELDS,
)


def _make_lock_surface_guard(lock_type, collector_error_type, label: str):
    getattr_fn = getattr
    expected_file_name = getattr_fn(lock_type, "FILE_NAME", None)
    expected_init = getattr_fn(lock_type, "__init__", None)
    expected_enter = getattr_fn(lock_type, "__enter__", None)
    expected_exit = getattr_fn(lock_type, "__exit__", None)
    expected_lock_handle = getattr_fn(lock_type, "_lock_handle", None)
    expected_lock_handle_code = getattr_fn(expected_lock_handle, "__code__", None)

    if not all(
        callable(item)
        for item in (
            expected_init,
            expected_enter,
            expected_exit,
            expected_lock_handle,
        )
    ) or expected_lock_handle_code is None:
        raise RuntimeError(f"collector {label} lock authority surface is incomplete")

    def require_lock_surface() -> None:
        if (
            getattr_fn(lock_type, "FILE_NAME", None) != expected_file_name
            or getattr_fn(lock_type, "__init__", None) is not expected_init
            or getattr_fn(lock_type, "__enter__", None) is not expected_enter
            or getattr_fn(lock_type, "__exit__", None) is not expected_exit
            or getattr_fn(lock_type, "_lock_handle", None) is not expected_lock_handle
            or getattr_fn(expected_lock_handle, "__code__", None)
            is not expected_lock_handle_code
        ):
            raise collector_error_type(
                f"collector {label} lock executable authority changed after composition"
            )

    return require_lock_surface


def _make_serialized_update(
    *,
    original_update,
    lock_type,
    fence_factory,
    lock_error_type,
    collector_error_type,
):
    getattr_fn = getattr
    original_code = getattr_fn(original_update, "__code__", None)
    fence_code = getattr_fn(fence_factory, "__code__", None)
    require_lock_surface = _make_lock_surface_guard(
        lock_type,
        collector_error_type,
        "state-mutation",
    )
    if original_code is None or fence_code is None:
        raise RuntimeError("collector state serialization executable authority is incomplete")

    def require_surfaces() -> None:
        require_lock_surface()
        if (
            getattr_fn(original_update, "__code__", None) is not original_code
            or getattr_fn(fence_factory, "__code__", None) is not fence_code
        ):
            raise collector_error_type(
                "collector state serialization executable authority changed after composition"
            )

    def serialized_update(self, mutate) -> None:
        require_surfaces()
        try:
            with lock_type(self.path.parent):
                original_update(self, fence_factory(mutate))
        except lock_error_type as exc:
            raise collector_error_type(
                "cannot serialize collector service durable state mutation"
            ) from exc
        finally:
            require_surfaces()

    return serialized_update


def _make_depth_resolver(cycle_depth):
    getattr_fn = getattr
    dict_type = dict

    def depths() -> dict[int, int]:
        values = getattr_fn(cycle_depth, "values", None)
        if values is None:
            values = dict_type()
            cycle_depth.values = values
        return values

    return depths


_depths = _make_depth_resolver(_CYCLE_DEPTH)


def _make_serialized_run_cycle(
    *,
    original_run_cycle,
    lock_type,
    depth_resolver,
    lock_error_type,
    collector_error_type,
):
    getattr_fn = getattr
    id_fn = id
    original_code = getattr_fn(original_run_cycle, "__code__", None)
    depth_code = getattr_fn(depth_resolver, "__code__", None)
    require_lock_surface = _make_lock_surface_guard(
        lock_type,
        collector_error_type,
        "cycle-mutation",
    )
    if original_code is None or depth_code is None:
        raise RuntimeError("collector cycle serialization executable authority is incomplete")

    def require_surfaces() -> None:
        require_lock_surface()
        if (
            getattr_fn(original_run_cycle, "__code__", None) is not original_code
            or getattr_fn(depth_resolver, "__code__", None) is not depth_code
        ):
            raise collector_error_type(
                "collector cycle serialization executable authority changed after composition"
            )

    def serialized_run_cycle(self):
        require_surfaces()
        try:
            identity = id_fn(self)
            depths = depth_resolver()
            depth = depths.get(identity, 0)
            if depth:
                depths[identity] = depth + 1
                try:
                    return original_run_cycle(self)
                finally:
                    if depths[identity] == 1:
                        depths.pop(identity, None)
                    else:
                        depths[identity] -= 1

            try:
                with lock_type(self._state.path.parent):
                    depths[identity] = 1
                    try:
                        return original_run_cycle(self)
                    finally:
                        depths.pop(identity, None)
            except lock_error_type as exc:
                raise collector_error_type(
                    "cannot serialize collector cycle mutation window"
                ) from exc
        finally:
            require_surfaces()

    return serialized_run_cycle


def _make_serialized_stop(
    *,
    original_stop,
    lock_type,
    depth_resolver,
    lock_error_type,
    collector_error_type,
):
    getattr_fn = getattr
    id_fn = id
    original_code = getattr_fn(original_stop, "__code__", None)
    depth_code = getattr_fn(depth_resolver, "__code__", None)
    require_lock_surface = _make_lock_surface_guard(
        lock_type,
        collector_error_type,
        "cycle-mutation",
    )
    if original_code is None or depth_code is None:
        raise RuntimeError("collector STOP serialization executable authority is incomplete")

    def require_surfaces() -> None:
        require_lock_surface()
        if (
            getattr_fn(original_stop, "__code__", None) is not original_code
            or getattr_fn(depth_resolver, "__code__", None) is not depth_code
        ):
            raise collector_error_type(
                "collector STOP serialization executable authority changed after composition"
            )

    def serialized_stop(self, reason: str = "operator_stop") -> None:
        require_surfaces()
        try:
            # `_stop_if_requested()` is invoked from inside run_cycle. Reacquiring
            # the same OS lock there would deadlock; that same-thread call already
            # owns the cycle mutation window and immediately raises _StopRequested.
            if depth_resolver().get(id_fn(self), 0):
                original_stop(self, reason)
                return
            try:
                with lock_type(self._state.path.parent):
                    original_stop(self, reason)
            except lock_error_type as exc:
                raise collector_error_type(
                    "cannot serialize collector STOP with in-flight cycle"
                ) from exc
        finally:
            require_surfaces()

    return serialized_stop


_serialized_update = _make_serialized_update(
    original_update=_ORIGINAL_UPDATE,
    lock_type=_CollectorServiceStateMutationLock,
    fence_factory=_fence_post_stop_mutation,
    lock_error_type=WorkspaceEconomicLockError,
    collector_error_type=_collector_service.CollectorServiceError,
)
_serialized_run_cycle = _make_serialized_run_cycle(
    original_run_cycle=_ORIGINAL_RUN_CYCLE,
    lock_type=_CollectorServiceCycleMutationLock,
    depth_resolver=_depths,
    lock_error_type=WorkspaceEconomicLockError,
    collector_error_type=_collector_service.CollectorServiceError,
)
_serialized_stop = _make_serialized_stop(
    original_stop=_ORIGINAL_STOP,
    lock_type=_CollectorServiceCycleMutationLock,
    depth_resolver=_depths,
    lock_error_type=WorkspaceEconomicLockError,
    collector_error_type=_collector_service.CollectorServiceError,
)


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
