from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .continuous_session import ContinuousSessionStatus, ContinuousTickResult
from .product_runtime import AutonomousProductRuntime


class ProductOperatorError(RuntimeError):
    """The operator lifecycle request is invalid for the current controller state."""


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
        # Validate canonical state at attachment time without creating a second local
        # lifecycle authority. Durable RUNNING/PAUSED/STOPPED remains the only truth.
        self._state_value(self._runtime.status())
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

    def _canonical_status(self) -> tuple[ContinuousSessionStatus, str]:
        status = self._runtime.status()
        return status, self._state_value(status)

    def start(self) -> ContinuousSessionStatus:
        """Start or resume the canonical runtime exactly once for this running phase."""

        with self._lock:
            self._ensure_open()
            status, state = self._canonical_status()
            if state == self._RUNNING:
                return status
            try:
                status = self._runtime.start()
            except Exception:
                # Runtime start spans multiple durable authorities: collector resume
                # can commit before session resume fails. Canonical STOP compensation
                # prevents a reported start failure from leaving a half-started graph.
                # Preserve the original start exception; callers keep the workspace
                # quarantined if the best-effort compensation itself also fails.
                try:
                    self._runtime.stop("operator_start_failed")
                except Exception:
                    pass
                raise
            self._state_value(status)
            return status

    def tick(self) -> ContinuousTickResult:
        """Execute exactly one canonical product tick; never schedules another tick."""

        with self._lock:
            self._ensure_open()
            _, state = self._canonical_status()
            if state != self._RUNNING:
                raise ProductOperatorError("product runtime must be started before tick")
            result = self._runtime.tick()
            self._controller_tick_count += 1
            return result

    def stop(self, reason: str = "operator_stop") -> ContinuousSessionStatus:
        """Persist an explicit operator stop without duplicating repeated stop requests."""

        normalized_reason = self._stop_reason(reason)
        with self._lock:
            self._ensure_open()
            status, state = self._canonical_status()
            if state == self._STOPPED:
                return status
            status = self._runtime.stop(normalized_reason)
            self._state_value(status)
            return status

    def status(self) -> ProductOperatorSnapshot:
        """Return controller state plus the canonical runtime's durable status."""

        with self._lock:
            canonical_status, canonical_state = self._canonical_status()
            state = self._CLOSED if self._closed else canonical_state
            return ProductOperatorSnapshot(
                state=state,
                controller_tick_count=self._controller_tick_count,
                canonical_status=canonical_status,
            )

    def close(self) -> None:
        """Release local runtime resources without inventing an implicit STOP event.

        Callers that want a durable operator STOP must call :meth:`stop` explicitly.
        Keeping close separate preserves truthful crash/error recovery semantics while
        still making cleanup idempotent.
        """

        with self._lock:
            if self._closed:
                return
            self._runtime.close()
            self._closed = True
