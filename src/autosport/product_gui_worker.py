from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .continuous_session import ContinuousSessionStatus, ContinuousTickResult
from .product_entrypoint import _validated_source
from .product_runtime import AutonomousProductRuntime, build_autonomous_product_runtime


RuntimeBuilder = Callable[[Path, str, str], AutonomousProductRuntime]


def _runtime_builder(
    workspace: Path,
    source_factory: str,
    initial_bankroll: str,
) -> AutonomousProductRuntime:
    """Build the canonical runtime through the same validated source boundary as CLI."""

    source = _validated_source(source_factory, workspace=workspace)
    return build_autonomous_product_runtime(
        workspace=workspace,
        source=source,
        initial_bankroll=initial_bankroll,
    )


def _safe_error_type(exc: BaseException) -> str:
    try:
        name = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        return "BaseException"
    return name if isinstance(name, str) and name else "BaseException"


@dataclass(frozen=True, slots=True)
class ProductGuiMessage:
    """One secret-safe UI projection from the canonical background product runtime."""

    kind: str
    status: ContinuousSessionStatus | None = None
    tick: ContinuousTickResult | None = None
    error_type: str | None = None
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"STARTED", "TICK", "STOPPED", "ERROR"}:
            raise ValueError("unsupported product GUI message kind")
        payload_count = sum(
            value is not None
            for value in (self.status, self.tick, self.error_type)
        )
        if payload_count != 1:
            raise ValueError("product GUI message must contain exactly one payload")
        if self.kind in {"STARTED", "STOPPED"} and self.status is None:
            raise ValueError("status message requires ContinuousSessionStatus")
        if self.kind == "TICK" and self.tick is None:
            raise ValueError("tick message requires ContinuousTickResult")
        if self.kind == "ERROR" and self.error_type is None:
            raise ValueError("error message requires error_type")
        if self.kind != "STOPPED" and self.stop_reason is not None:
            raise ValueError("stop_reason is valid only for STOPPED messages")


class ProductGuiWorker:
    """Own one non-daemon GUI thread driving the canonical durable PAPER runtime.

    The worker does not duplicate collector, market, PAPER, settlement, learning or
    economic authority. It only moves the existing ``AutonomousProductRuntime`` off
    the Tk/UIA event thread and provides a cooperative operator STOP boundary.
    """

    def __init__(self, *, runtime_builder: RuntimeBuilder = _runtime_builder) -> None:
        self._runtime_builder = runtime_builder
        self._messages: queue.Queue[ProductGuiMessage] = queue.Queue()
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._stop_reason = "operator_stop"

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(
        self,
        *,
        workspace: str | Path,
        source_factory: str,
        initial_bankroll: str = "10000",
        poll_seconds: float = 30.0,
    ) -> bool:
        if (
            type(source_factory) is not str
            or not source_factory
            or source_factory.strip() != source_factory
        ):
            raise ValueError("source_factory must be a non-empty trimmed string")
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(float(poll_seconds))
            or poll_seconds <= 0
        ):
            raise ValueError("poll_seconds must be a finite positive number")

        root = Path(workspace)
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._stop_event = threading.Event()
            self._stop_reason = "operator_stop"

        start_gate = threading.Event()
        cancelled = threading.Event()
        try:
            thread = threading.Thread(
                target=self._run_when_committed,
                args=(
                    root,
                    source_factory,
                    initial_bankroll,
                    float(poll_seconds),
                    start_gate,
                    cancelled,
                ),
                name="autosport-product-runtime-gui",
                daemon=False,
            )
        except BaseException:
            self._release_unstarted_slot()
            raise

        self._thread = thread
        committed = False
        try:
            thread.start()
            committed = True
            start_gate.set()
        except BaseException:
            if committed:
                start_gate.set()
                return True
            cancelled.set()
            start_gate.set()
            self._release_unstarted_slot()
            raise
        return True

    def request_stop(self, reason: str = "operator_stop") -> bool:
        if type(reason) is not str or not reason or reason.strip() != reason:
            raise ValueError("stop reason must be a non-empty trimmed string")
        with self._lock:
            if not self._busy:
                return False
            if not self._stop_event.is_set():
                self._stop_reason = reason
                self._stop_event.set()
            return True

    def poll(self) -> ProductGuiMessage | None:
        try:
            return self._messages.get_nowait()
        except queue.Empty:
            return None

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _release_unstarted_slot(self) -> None:
        self._thread = None
        with self._lock:
            self._busy = False

    def _run_when_committed(
        self,
        workspace: Path,
        source_factory: str,
        initial_bankroll: str,
        poll_seconds: float,
        start_gate: threading.Event,
        cancelled: threading.Event,
    ) -> None:
        start_gate.wait()
        if cancelled.is_set():
            return
        self._run(
            workspace=workspace,
            source_factory=source_factory,
            initial_bankroll=initial_bankroll,
            poll_seconds=poll_seconds,
        )

    def _run(
        self,
        *,
        workspace: Path,
        source_factory: str,
        initial_bankroll: str,
        poll_seconds: float,
    ) -> None:
        runtime: AutonomousProductRuntime | None = None
        started = False
        terminal_error: BaseException | None = None
        stopped_status: ContinuousSessionStatus | None = None
        stop_reason: str | None = None
        try:
            runtime = self._runtime_builder(workspace, source_factory, initial_bankroll)
            started_status = runtime.start()
            started = True
            self._messages.put(ProductGuiMessage(kind="STARTED", status=started_status))

            while not self._stop_event.is_set():
                tick = runtime.tick()
                self._messages.put(ProductGuiMessage(kind="TICK", tick=tick))
                if self._stop_event.wait(poll_seconds):
                    break

            stop_reason = self._stop_reason
            stopped_status = runtime.stop(stop_reason)
        except BaseException as exc:
            terminal_error = exc
            if runtime is not None and started:
                try:
                    runtime.stop("runtime_error")
                except BaseException:
                    pass
        finally:
            if runtime is not None:
                try:
                    runtime.close()
                except BaseException as exc:
                    if terminal_error is None:
                        terminal_error = exc
            if terminal_error is not None:
                self._messages.put(
                    ProductGuiMessage(
                        kind="ERROR",
                        error_type=_safe_error_type(terminal_error),
                    )
                )
            elif stopped_status is not None and stop_reason is not None:
                self._messages.put(
                    ProductGuiMessage(
                        kind="STOPPED",
                        status=stopped_status,
                        stop_reason=stop_reason,
                    )
                )
            with self._lock:
                self._busy = False
