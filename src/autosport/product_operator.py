from __future__ import annotations

from dataclasses import dataclass
from threading import Lock, RLock
from typing import Callable

from .collector_service import _SignalStopRequest
from .continuous_session import ContinuousSessionStatus, ContinuousTickResult
from .product_runtime import AutonomousProductRuntime


class ProductOperatorError(RuntimeError):
    """The operator lifecycle request is invalid for the current controller state."""


class _OperatorStopRequest(_SignalStopRequest):
    """Process-local STOP intent routed through the canonical collector cancellation port.

    This is deliberately not durable lifecycle state. It only lets an operator STOP
    become observable before the runtime operation fence can be acquired while an
    in-flight tick is still using that fence. Final STOP truth remains owned by the
    canonical collector/session runtime and is published only after safe quiescence.

    Subclassing the collector's existing signal request seam is intentional: once the
    canonical collector interruptible-backoff repair is present, the same operator
    token participates in its event-backed wait without adding a second cancellation
    mechanism.
    """

    def __init__(
        self,
        inherited_requested: Callable[[], bool] | None,
        inherited_reason: Callable[[], str] | None,
    ) -> None:
        super().__init__()
        self._reason_lock = RLock()
        self._operator_reason: str | None = None
        self._inherited_requested = inherited_requested
        self._inherited_reason = inherited_reason

    def __call__(self) -> bool:
        if self._event.is_set():
            return True
        inherited = self._inherited_requested
        return bool(inherited()) if callable(inherited) else False

    def request(self, reason: str) -> None:
        with self._reason_lock:
            self._operator_reason = reason
            self._event.set()

    def clear_operator_request(self) -> None:
        with self._reason_lock:
            self._event.clear()
            self._operator_reason = None

    def reason(self) -> str:
        with self._reason_lock:
            if self._event.is_set() and self._operator_reason is not None:
                return self._operator_reason
        inherited_requested = self._inherited_requested
        inherited_reason = self._inherited_reason
        if callable(inherited_requested) and inherited_requested():
            if callable(inherited_reason):
                return inherited_reason()
            return "stop_requested"
        return "stop_requested"

    def wait(self, timeout: float) -> bool:
        """Wait on operator intent, then re-check any inherited generic STOP source."""
        if self():
            return True
        self._event.wait(timeout)
        return self()


@dataclass(frozen=True, slots=True)
class ProductOperatorSnapshot:
    """Read-only operator projection over the canonical durable product runtime."""

    state: str
    controller_tick_count: int
    canonical_status: ContinuousSessionStatus


class ProductOperatorController:
    """Synchronous operator control for one canonical ``AutonomousProductRuntime``.

    The controller deliberately owns no scheduler, thread, sleep loop, market state,
    PAPER book, settlement path, or learning authority. A UI or CLI may schedule calls
    however it chooses, while every product mutation still flows through the single
    canonical runtime supplied at construction.

    Operator presentation is intentionally a projection of canonical durable session
    state. In particular, STOPPED is never reinterpreted as a process-local READY
    sentinel: the product runtime has no durable READY state that could justify that
    distinction after restart.
    """

    _RUNNING = "RUNNING"
    _PAUSED = "PAUSED"
    _STOPPED = "STOPPED"
    _CLOSED = "CLOSED"

    def __init__(self, runtime: AutonomousProductRuntime) -> None:
        if type(runtime) is not AutonomousProductRuntime:
            raise TypeError("runtime must be exact AutonomousProductRuntime")
        self._runtime = runtime
        self._lock = RLock()
        # Tick serialization is deliberately separate from the lifecycle lock. A
        # provider-facing tick can block for bounded I/O, but operator STOP intent must
        # still become visible immediately. Final durable STOP remains serialized by
        # AutonomousProductRuntime._operation_fence and the collector quiescence law.
        self._tick_lock = Lock()
        # Validate canonical state at attachment time without creating a second local
        # lifecycle authority. Durable RUNNING/PAUSED/STOPPED remains the only truth.
        initial_status = self._runtime.status()
        self._state_value(initial_status)
        self._last_canonical_status = initial_status

        collector = self._runtime.collector
        inherited_requested = getattr(collector, "stop_requested", None)
        inherited_reason = getattr(collector, "stop_reason", None)
        if isinstance(inherited_requested, _SignalStopRequest):
            # Replacing another event-backed signal owner would silently weaken its
            # wakeup semantics. Canonical product composition currently has no such
            # owner; fail closed rather than steal one if a custom runtime supplies it.
            raise ProductOperatorError(
                "canonical collector already owns an event-backed STOP request source"
            )
        self._stop_request = _OperatorStopRequest(
            inherited_requested if callable(inherited_requested) else None,
            inherited_reason if callable(inherited_reason) else None,
        )
        collector.stop_requested = self._stop_request
        collector.stop_reason = self._stop_request.reason

        self._closed = False
        self._controller_tick_count = 0

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProductOperatorError("product operator controller is closed")

    @staticmethod
    def _stop_reason(reason: object) -> str:
        if type(reason) is not str or not reason or reason.strip() != reason:
            raise ProductOperatorError("stop reason must be a non-empty trimmed string")
        return reason

    @classmethod
    def _state_value(cls, status: ContinuousSessionStatus) -> str:
        state = status.state
        value = state.value if hasattr(state, "value") else state
        if value not in {cls._RUNNING, cls._PAUSED, cls._STOPPED}:
            raise ProductOperatorError("canonical product runtime returned an invalid state")
        return value

    def _remember_status(self, status: ContinuousSessionStatus) -> str:
        state = self._state_value(status)
        self._last_canonical_status = status
        return state

    def _canonical_status(self) -> tuple[ContinuousSessionStatus, str]:
        status = self._runtime.status()
        return status, self._remember_status(status)

    def start(self) -> ContinuousSessionStatus:
        """Start or resume the canonical runtime exactly once for this running phase."""

        # Do not start/resume while a previous tick is still unwinding. STOP is the
        # sole lifecycle operation whose *intent* may preempt an in-flight tick.
        with self._tick_lock:
            with self._lock:
                self._ensure_open()
                if self._stop_request():
                    raise ProductOperatorError(
                        "pending STOP request requires durable recovery before start"
                    )
                status, state = self._canonical_status()
                if state == self._RUNNING:
                    return status
                # Canonical AutonomousProductRuntime owns START transaction rollback
                # and recovery. Do not issue a second controller-level STOP on failure.
                status = self._runtime.start()
                self._remember_status(status)
                return status

    def tick(self) -> ContinuousTickResult:
        """Execute exactly one canonical product tick; never schedules another tick.

        The lifecycle lock is released while the canonical tick executes. A separate
        tick lock keeps concurrent tick callers serialized. Operator STOP publishes a
        cooperative cancellation intent outside the runtime operation fence; durable
        STOP itself remains quiescence-serialized and is never raced against mutation.
        """

        with self._tick_lock:
            with self._lock:
                self._ensure_open()
                if self._stop_request():
                    raise ProductOperatorError(
                        "pending STOP request forbids another product tick"
                    )
                _, state = self._canonical_status()
                if state != self._RUNNING:
                    raise ProductOperatorError("product runtime must be started before tick")
            result = self._runtime.tick()
            with self._lock:
                self._controller_tick_count += 1
            return result

    def stop(self, reason: str = "operator_stop") -> ContinuousSessionStatus:
        """Request prompt cancellation, then publish durable STOP after quiescence."""

        normalized_reason = self._stop_reason(reason)
        # The request is deliberately published before any runtime status/STOP call.
        # Those calls acquire AutonomousProductRuntime._operation_fence and can wait
        # behind an in-flight tick. The collector sees this token through its existing
        # stop_requested/stop_reason port and can cooperatively abort at safe boundaries.
        with self._lock:
            self._ensure_open()
            self._stop_request.request(normalized_reason)

        try:
            with self._lock:
                self._ensure_open()
                try:
                    status, state = self._canonical_status()
                except Exception:
                    # Explicit STOP is also the canonical recovery path for a mixed
                    # collector/session graph. Do not let fail-closed status projection
                    # prevent the operator from reconciling both authorities to STOPPED.
                    status = self._runtime.stop(normalized_reason)
                    self._remember_status(status)
                    self._stop_request.clear_operator_request()
                    return status
                if state == self._STOPPED:
                    self._stop_request.clear_operator_request()
                    return status
                status = self._runtime.stop(normalized_reason)
                self._remember_status(status)
                self._stop_request.clear_operator_request()
                return status
        except BaseException:
            # A failed STOP keeps cancellation asserted. start()/tick() then fail closed
            # until canonical recovery completes instead of resurrecting a mixed graph.
            raise

    def status(self) -> ProductOperatorSnapshot:
        """Return controller state plus the latest canonical runtime status.

        Once the controller is closed, the underlying runtime has relinquished its
        workspace authority and may reject all further status reads. CLOSED therefore
        exposes only the last status successfully observed while runtime authority was
        still live; it is presentation history, not a second durable lifecycle state.
        """

        with self._lock:
            if self._closed:
                return ProductOperatorSnapshot(
                    state=self._CLOSED,
                    controller_tick_count=self._controller_tick_count,
                    canonical_status=self._last_canonical_status,
                )
            canonical_status, canonical_state = self._canonical_status()
            return ProductOperatorSnapshot(
                state=canonical_state,
                controller_tick_count=self._controller_tick_count,
                canonical_status=canonical_status,
            )

    def close(self) -> None:
        """Release local runtime resources without inventing an implicit STOP event.

        Callers that want a durable operator STOP must call :meth:`stop` explicitly.
        Keeping close separate preserves truthful crash/error recovery semantics while
        still making cleanup idempotent. A best-effort final canonical status refresh
        is retained only for CLOSED presentation; close itself is never blocked by an
        unreadable recovery state.
        """

        # Resource teardown cannot race a tick that may still be using the canonical
        # runtime graph. Unlike STOP intent, close waits for tick serialization to quiesce.
        with self._tick_lock:
            with self._lock:
                if self._closed:
                    return
                try:
                    self._canonical_status()
                except Exception:
                    # Cleanup must remain available even when canonical lifecycle status is
                    # already fail-closed (for example RECOVERY_REQUIRED). The previously
                    # validated status remains the only CLOSED presentation snapshot.
                    pass
                try:
                    self._runtime.close()
                except BaseException:
                    # Runtime close is an authority-revoking transition. After it has been
                    # attempted, never let this controller resurrect positive operations if
                    # a lower-level resource teardown reports a failure.
                    self._closed = True
                    raise
                self._closed = True
