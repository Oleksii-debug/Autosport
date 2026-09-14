from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO


class WorkspaceEconomicLockError(RuntimeError):
    """Raised when another process owns the workspace economic-writer lock."""


def _add_secondary_failure_note(
    primary: BaseException,
    prefix: str,
    secondary: BaseException,
) -> None:
    """Attach cleanup evidence without ever replacing the primary failure."""

    try:
        secondary_text = f"{type(secondary).__name__}: {secondary}"
    except BaseException:
        secondary_text = "secondary exception details unavailable"
    try:
        primary.add_note(f"{prefix}: {secondary_text}")
    except BaseException:
        # Exception subclasses may override add_note(), and diagnostic enrichment is
        # never allowed to replace the economic/replay failure we are preserving.
        return


class WorkspaceEconomicLock:
    """Cross-process, crash-releasing exclusive lock for PaperBook/ledger mutations.

    The lock is advisory and intentionally scoped to cooperating Autosport economic
    writers. Those writers treat the lock pathname as persistent workspace metadata:
    they do not unlink, rename, replace, or hard-link it while coordinating. Acquire
    rejects unsafe aliases and detects pathname replacement through its post-lock
    identity checkpoint. Like any pathname-based advisory file lock, it cannot make
    the pathname immutable against an external actor that replaces it after the final
    validated checkpoint; such filesystem mutation is outside this coordination
    contract. The file itself may persist after process exit; the operating-system
    lock is the authority and is released automatically when the owning process dies.
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
        handle = self._open_lock_handle()
        try:
            # The creation/open split never creates or truncates through an existing
            # pathname. Bind the opened handle back to the exact canonical workspace
            # pathname before any sentinel byte is written so a race-created alias
            # cannot mutate an external file during lock initialization.
            self._validate_open_handle_identity(handle)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            self._lock_handle(handle)
            # Detect replacement that happened between open and this post-lock
            # checkpoint. This is deliberately not a claim that an external actor
            # cannot replace a POSIX pathname after the checkpoint; cooperating
            # Autosport writers never perform that mutation (see class contract).
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
                _add_secondary_failure_note(
                    acquire_error,
                    "workspace economic lock handle close also failed while cleaning up acquisition failure",
                    close_error,
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
                _add_secondary_failure_note(
                    unlock_error,
                    "workspace economic lock handle close also failed after unlock failure",
                    close_error,
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
            # a secondary lock-teardown failure. Diagnostic enrichment itself is an
            # untrusted exception boundary, so it is deliberately best-effort.
            _add_secondary_failure_note(
                exc_value,
                "WorkspaceEconomicLock release also failed while propagating the primary error",
                release_error,
            )

    def _open_lock_handle(self) -> BinaryIO:
        """Create or open the canonical lock without create-through-alias races."""

        try:
            return self.path.open("x+b")
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "cannot create workspace economic lock path"
            ) from exc

        self._validate_existing_lock_path()
        try:
            return self.path.open("r+b")
        except FileNotFoundError as exc:
            raise WorkspaceEconomicLockError(
                "workspace economic lock path changed during acquisition"
            ) from exc
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "cannot open workspace economic lock path"
            ) from exc

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
