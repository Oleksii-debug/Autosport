from __future__ import annotations

import math
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .continuous_session import (
    ContinuousSessionStatus,
    ContinuousTickResult,
    SessionStoppedError,
)
from .product_entrypoint import ProductEntrypointError, _validated_source
from .product_runtime import AutonomousProductRuntime, build_autonomous_product_runtime


RuntimeBuilder = Callable[[Path, str, str], AutonomousProductRuntime]


def _runtime_builder(
    workspace: Path,
    source_factory: str,
    initial_bankroll: str,
    *,
    expected_source_id: str | None = None,
) -> AutonomousProductRuntime:
    """Build the canonical runtime through the same validated source boundary as CLI."""

    if expected_source_id is not None and (
        type(expected_source_id) is not str
        or not expected_source_id
        or expected_source_id.strip() != expected_source_id
    ):
        raise ValueError("expected_source_id must be a non-empty trimmed string")
    source = _validated_source(source_factory, workspace=workspace)
    if expected_source_id is not None and source.source_id != expected_source_id:
        raise ProductEntrypointError(
            "product source identity does not match the configured source"
        )
    return build_autonomous_product_runtime(
        workspace=workspace,
        source=source,
        initial_bankroll=initial_bankroll,
    )


_CANONICAL_RUNTIME_BUILDER = _runtime_builder


def _safe_error_type(exc: BaseException) -> str:
    """Return a bounded identifier only; exception detail never crosses to the UI."""

    try:
        name = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        return "BaseException"
    if (
        type(name) is not str
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) is None
    ):
        return "BaseException"
    return name


@dataclass(frozen=True, slots=True)
class ProductGuiMessage:
    """One secret-safe projection from the canonical background product runtime."""

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
    """Drive one canonical durable PAPER runtime off the WebView/GUI thread.

    This class owns no market, money, settlement, provider, learning, or execution
    semantics. It only serializes lifetime of the existing AutonomousProductRuntime
    and projects bounded messages to presentation.
    """

    def __init__(self, *, runtime_builder: RuntimeBuilder = _runtime_builder) -> None:
        self._runtime_builder = runtime_builder
        self._messages: queue.Queue[ProductGuiMessage] = queue.Queue()
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None
        self._runtime: AutonomousProductRuntime | None = None
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
        expected_source_id: str | None = None,
        initial_bankroll: str = "10000",
        poll_seconds: float = 30.0,
    ) -> bool:
        if (
            type(source_factory) is not str
            or not source_factory
            or source_factory.strip() != source_factory
        ):
            raise ValueError("source_factory must be a non-empty trimmed string")
        if expected_source_id is not None and (
            type(expected_source_id) is not str
            or not expected_source_id
            or expected_source_id.strip() != expected_source_id
        ):
            raise ValueError("expected_source_id must be a non-empty trimmed string")
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
            self._messages = queue.Queue()
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
                    expected_source_id,
                    initial_bankroll,
                    float(poll_seconds),
                    start_gate,
                    cancelled,
                ),
                name="autosport-product-runtime-webview",
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
        runtime: AutonomousProductRuntime | None
        resolved_reason: str
        with self._lock:
            if not self._busy:
                return False
            if not self._stop_event.is_set():
                self._stop_reason = reason
                self._stop_event.set()
            resolved_reason = self._stop_reason
            runtime = self._runtime

        if runtime is not None:
            request_runtime_stop = getattr(runtime, "request_stop", None)
            if callable(request_runtime_stop):
                request_runtime_stop(resolved_reason)
        return True

    def poll(self) -> ProductGuiMessage | None:
        try:
            message = self._messages.get_nowait()
        except queue.Empty:
            return None
        if message.kind in {"STOPPED", "ERROR"}:
            # A terminal message is part of runtime lifecycle truth, not optional
            # telemetry. Keep the admission slot owned until presentation consumes
            # it so a successor start cannot replace the queue and erase terminal
            # STOP/ERROR evidence before the controller applies recovery/status truth.
            with self._lock:
                self._busy = False
        return message

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
        expected_source_id: str | None,
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
            expected_source_id=expected_source_id,
            initial_bankroll=initial_bankroll,
            poll_seconds=poll_seconds,
        )

    def _run(
        self,
        *,
        workspace: Path,
        source_factory: str,
        expected_source_id: str | None,
        initial_bankroll: str,
        poll_seconds: float,
    ) -> None:
        runtime: AutonomousProductRuntime | None = None
        terminal_error: BaseException | None = None
        stopped_status: ContinuousSessionStatus | None = None
        stop_reason: str | None = None
        try:
            if expected_source_id is None:
                runtime = self._runtime_builder(
                    workspace,
                    source_factory,
                    initial_bankroll,
                )
            else:
                if self._runtime_builder is not _CANONICAL_RUNTIME_BUILDER:
                    raise ProductEntrypointError(
                        "configured source identity requires the canonical runtime builder"
                    )
                runtime = _CANONICAL_RUNTIME_BUILDER(
                    workspace,
                    source_factory,
                    initial_bankroll,
                    expected_source_id=expected_source_id,
                )
            with self._lock:
                self._runtime = runtime

            # A STOP requested while construction was in flight must win before START.
            if self._stop_event.is_set():
                stop_reason = self._stop_reason
                stopped_status = runtime.stop(stop_reason)
            else:
                started_status = runtime.start()
                self._messages.put(
                    ProductGuiMessage(kind="STARTED", status=started_status)
                )

                while not self._stop_event.is_set():
                    try:
                        tick = runtime.tick()
                    except SessionStoppedError:
                        if not self._stop_event.is_set():
                            raise
                        break
                    if self._stop_event.is_set():
                        break
                    self._messages.put(ProductGuiMessage(kind="TICK", tick=tick))
                    if self._stop_event.wait(poll_seconds):
                        break

                stop_reason = self._stop_reason
                stopped_status = runtime.stop(stop_reason)
        except BaseException as exc:
            terminal_error = exc
            # Compensation is based on possession of a canonical runtime, not on
            # successful return from start(). This closes the in-process partial-START
            # hole while the durable crash-window recovery remains owned by #821.
            if runtime is not None:
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
            terminal_message: ProductGuiMessage | None = None
            if terminal_error is not None:
                terminal_message = ProductGuiMessage(
                    kind="ERROR",
                    error_type=_safe_error_type(terminal_error),
                )
            elif stopped_status is not None and stop_reason is not None:
                terminal_message = ProductGuiMessage(
                    kind="STOPPED",
                    status=stopped_status,
                    stop_reason=stop_reason,
                )

            # Revoke the runtime reference before exposing terminal truth. The busy
            # slot deliberately remains held until poll() consumes STOPPED/ERROR;
            # otherwise a successor start can replace _messages in the publication
            # gap and permanently drop the prior run's terminal state.
            with self._lock:
                self._runtime = None
                if terminal_message is None:
                    self._busy = False
            if terminal_message is not None:
                self._messages.put(terminal_message)
