"""Nonblocking two-file support export for the existing Windows operator UI."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path

from .operator_support import OperatorSupportTrace


@dataclass(frozen=True, slots=True)
class SupportExportMessage:
    completed: bool
    files: tuple[str, str] | None = None
    error_code: str | None = None


class OneShotSupportExportWorker:
    """Return bounded statuses; never forward exception prose or exported paths."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._queue: queue.Queue[SupportExportMessage] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(
        self,
        trace: OperatorSupportTrace,
        directory: str | Path,
        *,
        strategy: str,
        research_plan_loaded: bool,
    ) -> bool:
        if not isinstance(trace, OperatorSupportTrace):
            return False
        if type(research_plan_loaded) is not bool:
            return False
        try:
            parent = Path(directory)
        except (TypeError, ValueError, OSError):
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        try:
            thread = threading.Thread(
                target=self._run,
                args=(trace, parent, strategy, research_plan_loaded),
                name="autosport-support-export",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        except Exception:
            with self._lock:
                self._busy = False
            return False
        return True

    def _run(
        self,
        trace: OperatorSupportTrace,
        directory: Path,
        strategy: str,
        research_plan_loaded: bool,
    ) -> None:
        try:
            paths = trace.export_reports(
                directory,
                selected_strategy=strategy,
                research_plan_loaded=research_plan_loaded,
            )
            message = SupportExportMessage(completed=True, files=(paths[0].name, paths[1].name))
        except Exception:
            message = SupportExportMessage(completed=False, error_code="SUPPORT_EXPORT_FAILED")
        self._queue.put(message)

    def poll(self) -> SupportExportMessage | None:
        try:
            result = self._queue.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return result
