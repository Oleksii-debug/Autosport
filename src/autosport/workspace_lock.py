from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class WorkspaceEconomicLockError(RuntimeError):
    """Raised when another process owns the workspace economic-writer lock."""


class WorkspaceEconomicLock:
    """Cross-process, crash-releasing exclusive lock for PaperBook/ledger mutations.

    The lock is advisory and intentionally scoped to Autosport's economic writers.
    The lock file itself may persist after process exit; the operating-system lock
    is the authority and is released automatically when the owning process dies.
    """

    FILE_NAME = ".economic-run.lock"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise WorkspaceEconomicLockError("workspace economic lock is already held by this lock object")
        self.workspace.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            self._lock_handle(handle)
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._unlock_handle(handle)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "WorkspaceEconomicLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkspaceEconomicLockError(
                    "another Autosport process owns the workspace economic-writer lock"
                ) from exc
            return

        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "another Autosport process owns the workspace economic-writer lock"
            ) from exc

    @staticmethod
    def _unlock_handle(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
