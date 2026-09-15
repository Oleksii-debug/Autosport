from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable

from .dataset import ReplayDataset


DatasetValidationTask = Callable[[], ReplayDataset]


def _safe_worker_error(exc: BaseException) -> str:
    """Render a terminal worker failure without trusting exception metadata."""

    try:
        name = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        name = "BaseException"
    try:
        detail = str(exc)
    except BaseException:
        return f"{name}: dataset validation failed; exception details unavailable"
    return f"{name}: {detail}" if detail else name


@dataclass(frozen=True, slots=True)
class DatasetValidationMessage:
    result: ReplayDataset | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("worker message must contain exactly one of result or error")


class OneShotDatasetValidationWorker:
    """Validate one read-only replay dataset away from the Tk/UIA event thread.

    Dataset validation reads and hashes source bytes but does not publish or mutate
    product state. The thread is therefore deliberately daemonized: closing the app
    may abandon an in-flight validation without risking an economic or persistence
    transaction. A start gate prevents a partially-started thread from running the
    task when ``Thread.start()`` itself reports failure.
    """

    def __init__(self) -> None:
        self._messages: queue.Queue[DatasetValidationMessage] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, task: DatasetValidationTask) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        try:
            start_gate = threading.Event()
            cancelled = threading.Event()
            thread = threading.Thread(
                target=self._run_when_committed,
                args=(task, start_gate, cancelled),
                name="autosport-dataset-validation",
                daemon=True,
            )
        except BaseException as exc:
            self._release_unstarted_slot()
            if isinstance(exc, Exception):
                return False
            raise

        self._thread = thread
        committed = False
        try:
            thread.start()
            committed = True
            start_gate.set()
        except BaseException as exc:
            if committed:
                start_gate.set()
                if isinstance(exc, Exception):
                    return True
                raise

            cancelled.set()
            start_gate.set()
            self._release_unstarted_slot()
            if isinstance(exc, Exception):
                return False
            raise
        return True

    def _release_unstarted_slot(self) -> None:
        self._thread = None
        with self._lock:
            self._busy = False

    def _run_when_committed(
        self,
        task: DatasetValidationTask,
        start_gate: threading.Event,
        cancelled: threading.Event,
    ) -> None:
        start_gate.wait()
        if cancelled.is_set():
            return
        self._run(task)

    def _run(self, task: DatasetValidationTask) -> None:
        try:
            message = DatasetValidationMessage(result=task())
        except BaseException as exc:
            message = DatasetValidationMessage(error=_safe_worker_error(exc))
        self._messages.put(message)

    def poll(self) -> DatasetValidationMessage | None:
        try:
            message = self._messages.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return message
