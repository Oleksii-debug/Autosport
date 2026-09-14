from __future__ import annotations

import os
import stat
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
        self._validate_existing_lock_path()
        handle = self.path.open("a+b")
        try:
            # Path.open() follows symlinks. Bind the opened handle back to the exact
            # canonical workspace pathname before any sentinel byte is written so an
            # unsafe alias cannot mutate an external file during lock initialization.
            self._validate_open_handle_identity(handle)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            self._lock_handle(handle)
            # A pathname can be replaced between open and OS-lock acquisition. Recheck
            # immediately after locking so successful acquire() never reports ownership
            # of a different inode than the canonical workspace lock pathname.
            self._validate_open_handle_identity(handle)
        except BaseException as acquire_error:
            try:
                handle.close()
            except BaseException as close_error:
                # Closing the handle is the only generic cleanup that can prove any
                # partially-acquired OS lock is gone. If that proof fails, retain
                # the handle as a poisoned ownership marker so this object cannot
                # silently reserve another writer slot.
                self._handle = handle
                acquire_error.add_note(
                    "workspace economic lock handle close also failed while cleaning up acquisition failure: "
                    f"{type(close_error).__name__}: {close_error}"
                )
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._unlock_handle(handle)
        except BaseException as unlock_error:
            # Closing the handle is the final OS-level release fallback. Detach only
            # if close succeeds. If both unlock and close fail, ownership is unknown
            # and the retained handle keeps this object fail-closed/non-reusable.
            try:
                handle.close()
            except BaseException as close_error:
                unlock_error.add_note(
                    "workspace economic lock handle close also failed after unlock failure: "
                    f"{type(close_error).__name__}: {close_error}"
                )
                self._handle = handle
            else:
                self._handle = None
            raise
        try:
            handle.close()
        finally:
            # Explicit unlock succeeded, so OS lock release is proven even if
            # closing the now-unlocked file handle itself reports an error.
            self._handle = None

    def __enter__(self) -> "WorkspaceEconomicLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_value is None:
            # A release failure after a successful body is itself a run failure and
            # must remain observable to the caller.
            self.release()
            return
        try:
            self.release()
        except BaseException as release_error:
            # Never replace the economic/replay failure that caused scope exit with
            # a secondary lock-teardown failure. Keep both pieces of evidence on the
            # primary exception so recovery diagnostics retain the actual root cause.
            exc_value.add_note(
                "WorkspaceEconomicLock release also failed while propagating the primary error: "
                f"{type(release_error).__name__}: {release_error}"
            )

    def _validate_existing_lock_path(self) -> None:
        """Reject unsafe aliases before opening the canonical lock pathname."""

        try:
            path_stat = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "cannot inspect workspace economic lock path"
            ) from exc
        self._require_regular_file(path_stat)
        self._require_single_link(path_stat)

    def _validate_open_handle_identity(self, handle: BinaryIO) -> None:
        """Prove the opened handle is the current canonical single-link lock file."""

        try:
            opened_stat = os.fstat(handle.fileno())
            path_stat = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "workspace economic lock path changed during acquisition"
            ) from exc
        self._require_regular_file(opened_stat)
        self._require_regular_file(path_stat)
        if not os.path.samestat(opened_stat, path_stat):
            raise WorkspaceEconomicLockError(
                "workspace economic lock path changed during acquisition"
            )
        # Only after proving both stat snapshots identify the same inode can link
        # count describe aliases of the canonical lock rather than an unlinked old
        # handle from a pathname-replacement race.
        self._require_single_link(opened_stat)
        self._require_single_link(path_stat)

    @staticmethod
    def _require_regular_file(path_stat: os.stat_result) -> None:
        if not stat.S_ISREG(path_stat.st_mode):
            raise WorkspaceEconomicLockError(
                "workspace economic lock path must be a regular non-symlink file"
            )

    @staticmethod
    def _require_single_link(path_stat: os.stat_result) -> None:
        if path_stat.st_nlink != 1:
            raise WorkspaceEconomicLockError(
                "workspace economic lock path must not have hard-link aliases"
            )

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
