from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path

from .evidence_export import _resolve_output_destination, export_evidence_manifest


def _safe_worker_error(exc: BaseException) -> str:
    """Return a bounded structural failure code without user-visible prose."""

    try:
        name = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        name = "BaseException"
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 96
        or not name.replace("_", "").isalnum()
    ):
        name = "BaseException"
    return name


def resolve_evidence_output_destination(
    workspace: str | Path,
    output: str | Path,
) -> Path:
    """Preflight a GUI destination through the canonical exporter destination law."""

    return _resolve_output_destination(Path(workspace), Path(output))


@dataclass(frozen=True, slots=True)
class EvidenceExportMessage:
    output: Path | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.output is None) == (self.error is None):
            raise ValueError("worker message must contain exactly one of output or error")


class OneShotEvidenceExportWorker:
    """Export one canonical evidence manifest away from the Tk/UIA event thread.

    The canonical exporter owns destination fencing, manifest semantics and
    secret-safety. This adapter only serializes one export at a time and reports
    one terminal message for Tk polling.
    """

    def __init__(self) -> None:
        self._messages: queue.Queue[EvidenceExportMessage] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, workspace: str | Path, output: str | Path) -> bool:
        # Convert caller values before owning the worker slot. A hostile or invalid
        # path-like value must not strand ``busy=True`` before a thread exists.
        try:
            workspace_path = Path(workspace)
            output_path = Path(output)
        except (TypeError, ValueError, OSError):
            return False

        with self._lock:
            if self._busy:
                return False
            self._busy = True

        try:
            start_gate = threading.Event()
            cancelled = threading.Event()
            thread = threading.Thread(
                target=self._run_when_committed,
                args=(workspace_path, output_path, start_gate, cancelled),
                name="autosport-evidence-export",
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
        workspace: Path,
        output: Path,
        start_gate: threading.Event,
        cancelled: threading.Event,
    ) -> None:
        start_gate.wait()
        if cancelled.is_set():
            return
        self._run(workspace, output)

    def _run(self, workspace: Path, output: Path) -> None:
        try:
            export_evidence_manifest(workspace, output)
            message = EvidenceExportMessage(output=output)
        except BaseException as exc:
            message = EvidenceExportMessage(error=_safe_worker_error(exc))
        self._messages.put(message)

    def poll(self) -> EvidenceExportMessage | None:
        try:
            message = self._messages.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return message
