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
    PAPER book, settlement path, or learning authority.  A UI or CLI may schedule
    calls however it chooses, while every product mutation still flows through the
    single canonical runtime supplied at construction.
    """

    _READY = "READY"
    _RUNNING = "RUNNING"
    _STOPPED = "STOPPED"
    _CLOSED = "CLOSED"

    def __init__(self, runtime: AutonomousProductRuntime) -> None:
        if type(runtime) is not AutonomousProductRuntime:
            raise TypeError("runtime must be exact AutonomousProductRuntime")
        self._runtime = runtime
        self._lock = RLock()
        self._running = False
        self._ever_started = False
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

    def start(self) -> ContinuousSessionStatus:
        """Start or resume the canonical runtime exactly once for this running phase."""

        with self._lock:
            self._ensure_open()
            if self._running:
                return self._runtime.status()
            status = self._runtime.start()
            self._running = True
            self._ever_started = True
            return status

    def tick(self) -> ContinuousTickResult:
        """Execute exactly one canonical product tick; never schedules another tick."""

        with self._lock:
            self._ensure_open()
            if not self._running:
                raise ProductOperatorError("product runtime must be started before tick")
            result = self._runtime.tick()
            self._controller_tick_count += 1
            return result

    def stop(self, reason: str = "operator_stop") -> ContinuousSessionStatus:
        """Persist an explicit operator stop without duplicating repeated stop requests."""

        normalized_reason = self._stop_reason(reason)
        with self._lock:
            self._ensure_open()
            if not self._running:
                return self._runtime.status()
            status = self._runtime.stop(normalized_reason)
            self._running = False
            return status

    def status(self) -> ProductOperatorSnapshot:
        """Return controller state plus the canonical runtime's durable status."""

        with self._lock:
            if self._closed:
                state = self._CLOSED
            elif self._running:
                state = self._RUNNING
            elif self._ever_started:
                state = self._STOPPED
            else:
                state = self._READY
            return ProductOperatorSnapshot(
                state=state,
                controller_tick_count=self._controller_tick_count,
                canonical_status=self._runtime.status(),
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
            self._running = False
