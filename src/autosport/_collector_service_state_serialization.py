from __future__ import annotations

"""Compose collector STOP serialization with the current prospective schedule surface.

The canonical #1373 state/quiescence implementation is preserved byte-for-byte in
``_collector_service_state_serialization_base``.  Current main subsequently widened
``HeadlessCollectorService.run_cycle`` with the private canonical ``_schedule_slot``
argument owned by #1180.  This composition layer keeps the exact #1373 locks/depth
authority while forwarding that one current-main argument, and installs the existing
signal STOP as the single interruptible-wait authority for provider backoff.

No scheduler, lifecycle, state store, provider authority, or durable STOP authority is
created here.  The underlying state/update/STOP wrappers remain owned by the canonical
#1373 base implementation.
"""

import math

from . import _collector_service_state_serialization_base as _base
from . import collector_service as _collector_service

# Re-export the inspectable canonical #1373 surfaces used by its adversarial tests.
# These are aliases to the same objects; the installed wrappers below closure-capture
# their exact dependencies and therefore do not dispatch through these writable names.
_ORIGINAL_UPDATE = _base._ORIGINAL_UPDATE
_ORIGINAL_RUN_CYCLE = _base._ORIGINAL_RUN_CYCLE
_ORIGINAL_STOP = _base._ORIGINAL_STOP
_fence_post_stop_mutation = _base._fence_post_stop_mutation
_CollectorServiceStateMutationLock = _base._CollectorServiceStateMutationLock
_CollectorServiceCycleMutationLock = _base._CollectorServiceCycleMutationLock
_depths = _base._depths

_Service = _collector_service.HeadlessCollectorService
_SignalStopRequest = _collector_service._SignalStopRequest
_CollectorServiceError = _collector_service.CollectorServiceError
_ProviderUnavailableError = _collector_service.ProviderUnavailableError

_CURRENT_RUN_CYCLE_ANCHOR = "_collector_service_serialized_run_cycle_v2_schedule_compat"
_SIGNAL_WAIT_ANCHOR = "_collector_service_signal_stop_wait_v1"
_ORIGINAL_PROVIDER_CALL_ANCHOR = "_collector_service_original_bounded_provider_call_v1"
_INTERRUPTIBLE_PROVIDER_CALL_ANCHOR = "_collector_service_interruptible_bounded_provider_call_v1"


def _make_current_run_cycle_wrapper(
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
    require_lock_surface = _base._make_lock_surface_guard(
        lock_type,
        collector_error_type,
        "cycle-mutation",
    )
    if original_code is None or depth_code is None:
        raise RuntimeError("collector schedule-compatible cycle authority is incomplete")

    def require_surfaces() -> None:
        require_lock_surface()
        if (
            getattr_fn(original_run_cycle, "__code__", None) is not original_code
            or getattr_fn(depth_resolver, "__code__", None) is not depth_code
        ):
            raise collector_error_type(
                "collector schedule-compatible cycle authority changed after composition"
            )

    def serialized_run_cycle(self, *, _schedule_slot=None):
        require_surfaces()
        try:
            identity = id_fn(self)
            depths = depth_resolver()
            depth = depths.get(identity, 0)
            if depth:
                depths[identity] = depth + 1
                try:
                    return original_run_cycle(self, _schedule_slot=_schedule_slot)
                finally:
                    if depths[identity] == 1:
                        depths.pop(identity, None)
                    else:
                        depths[identity] -= 1

            try:
                with lock_type(self._state.path.parent):
                    depths[identity] = 1
                    try:
                        return original_run_cycle(self, _schedule_slot=_schedule_slot)
                    finally:
                        depths.pop(identity, None)
            except lock_error_type as exc:
                raise collector_error_type(
                    "cannot serialize collector cycle mutation window"
                ) from exc
        finally:
            require_surfaces()

    return serialized_run_cycle


def _make_signal_wait():
    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)

    return wait


def _make_interruptible_provider_call(
    *,
    original_provider_call,
    stop_check,
    signal_type,
    signal_wait,
    provider_error_type,
    collector_error_type,
):
    getattr_fn = getattr
    isinstance_fn = isinstance
    float_fn = float
    original_code = getattr_fn(original_provider_call, "__code__", None)
    stop_code = getattr_fn(stop_check, "__code__", None)
    wait_code = getattr_fn(signal_wait, "__code__", None)
    if original_code is None or stop_code is None or wait_code is None:
        raise RuntimeError("collector interruptible backoff authority is incomplete")

    def require_surfaces() -> None:
        if (
            getattr_fn(original_provider_call, "__code__", None) is not original_code
            or getattr_fn(stop_check, "__code__", None) is not stop_code
            or getattr_fn(signal_wait, "__code__", None) is not wait_code
            or getattr_fn(signal_type, "wait", None) is not signal_wait
        ):
            raise collector_error_type(
                "collector interruptible backoff executable authority changed after composition"
            )

    def interruptible_provider_call(self, action):
        require_surfaces()
        delay = self.config.initial_backoff_seconds
        for attempt in range(self.config.retry_attempts):
            stop_check(self)
            try:
                return action()
            except provider_error_type:
                if attempt + 1 >= self.config.retry_attempts:
                    raise
                stop_check(self)
                random_value = self.random_value()
                if (
                    isinstance_fn(random_value, bool)
                    or not isinstance_fn(random_value, (int, float))
                    or not math.isfinite(random_value)
                    or not 0 <= random_value <= 1
                ):
                    raise collector_error_type(
                        "random_value must return a finite number in [0, 1]"
                    )
                jittered = delay * (
                    1 + self.config.jitter_fraction * float_fn(random_value)
                )
                backoff = min(self.config.max_backoff_seconds, jittered)
                stop_source = self.stop_requested
                if isinstance_fn(stop_source, signal_type):
                    wait_result = stop_source.wait(backoff)
                    if type(wait_result) is not bool:
                        raise collector_error_type(
                            "canonical signal STOP wait must return bool"
                        )
                    stop_check(self)
                else:
                    self.sleep(backoff)
                delay = min(self.config.max_backoff_seconds, delay * 2)
        raise AssertionError("unreachable retry loop")

    return interruptible_provider_call


# The base guard already captured the exact current-main original run_cycle before it
# installed its predecessor-signature wrapper.  Reuse that anchor and the exact same
# lock/depth objects so STOP quiescence and scheduled START share one mutation fence.
_current_run_cycle = _make_current_run_cycle_wrapper(
    original_run_cycle=_base._ORIGINAL_RUN_CYCLE,
    lock_type=_base._CollectorServiceCycleMutationLock,
    depth_resolver=_base._depths,
    lock_error_type=_base.WorkspaceEconomicLockError,
    collector_error_type=_CollectorServiceError,
)
installed_cycle = getattr(_collector_service, _CURRENT_RUN_CYCLE_ANCHOR, None)
if installed_cycle is None:
    installed_cycle = _current_run_cycle
    setattr(_collector_service, _CURRENT_RUN_CYCLE_ANCHOR, installed_cycle)
if not callable(installed_cycle):
    raise RuntimeError("collector current cycle authority anchor is invalid")
_Service.run_cycle = installed_cycle
# Keep the original #1373 anchor aligned with the actually installed canonical wrapper
# so a later idempotent composition cannot restore the predecessor signature.
setattr(_collector_service, _base._SERIALIZED_RUN_CYCLE_ANCHOR, installed_cycle)


_signal_wait = getattr(_collector_service, _SIGNAL_WAIT_ANCHOR, None)
if _signal_wait is None:
    _signal_wait = _make_signal_wait()
    setattr(_collector_service, _SIGNAL_WAIT_ANCHOR, _signal_wait)
if not callable(_signal_wait):
    raise RuntimeError("collector signal STOP wait anchor is invalid")
_SignalStopRequest.wait = _signal_wait

_original_provider_call = getattr(
    _collector_service,
    _ORIGINAL_PROVIDER_CALL_ANCHOR,
    None,
)
if _original_provider_call is None:
    _original_provider_call = _Service._bounded_provider_call
    setattr(
        _collector_service,
        _ORIGINAL_PROVIDER_CALL_ANCHOR,
        _original_provider_call,
    )
if not callable(_original_provider_call):
    raise RuntimeError("collector provider-call authority anchor is invalid")

_interruptible_provider_call = _make_interruptible_provider_call(
    original_provider_call=_original_provider_call,
    stop_check=_Service._stop_if_requested,
    signal_type=_SignalStopRequest,
    signal_wait=_signal_wait,
    provider_error_type=_ProviderUnavailableError,
    collector_error_type=_CollectorServiceError,
)
installed_provider_call = getattr(
    _collector_service,
    _INTERRUPTIBLE_PROVIDER_CALL_ANCHOR,
    None,
)
if installed_provider_call is None:
    installed_provider_call = _interruptible_provider_call
    setattr(
        _collector_service,
        _INTERRUPTIBLE_PROVIDER_CALL_ANCHOR,
        installed_provider_call,
    )
if not callable(installed_provider_call):
    raise RuntimeError("collector interruptible provider-call anchor is invalid")
_Service._bounded_provider_call = installed_provider_call


__all__: list[str] = []
