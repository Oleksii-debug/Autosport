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
            return self._open_new_lock_handle()
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

    def _open_new_lock_handle(self) -> BinaryIO:
        """Exclusively create the canonical lock without following Windows reparse points."""

        if os.name != "nt":
            return self.path.open("x+b")

        # Python's CRT-backed x+b can follow a Windows symlink/reparse point whose
        # target does not exist, creating that external target before our identity
        # checks run. CreateFileW with OPEN_REPARSE_POINT makes the final pathname
        # component authoritative: an existing reparse point causes CREATE_NEW to
        # fail instead of being traversed. Existing files are opened separately,
        # without create/truncate semantics, after the no-follow path checks below.
        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE

        generic_read = 0x80000000
        generic_write = 0x40000000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        create_new = 1
        file_attribute_normal = 0x00000080
        file_flag_open_reparse_point = 0x00200000
        invalid_handle_value = ctypes.c_void_p(-1).value

        kernel_handle = create_file(
            str(self.path),
            generic_read | generic_write,
            file_share_read | file_share_write | file_share_delete,
            None,
            create_new,
            file_attribute_normal | file_flag_open_reparse_point,
            None,
        )
        if kernel_handle == invalid_handle_value:
            error_code = ctypes.get_last_error()
            if error_code in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise FileExistsError(
                    error_code,
                    "workspace economic lock path already exists",
                    str(self.path),
                )
            raise ctypes.WinError(error_code)

        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        try:
            descriptor = msvcrt.open_osfhandle(
                kernel_handle,
                os.O_RDWR | os.O_BINARY,
            )
        except BaseException:
            close_handle(kernel_handle)
            raise
        try:
            return os.fdopen(descriptor, "r+b", closefd=True)
        except BaseException:
            os.close(descriptor)
            raise

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
            opened_before = os.fstat(handle.fileno())
            path_before = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "workspace economic lock path changed during acquisition"
            ) from exc
        self._require_regular_file(opened_before)
        self._require_regular_file(path_before)
        self._require_single_link(opened_before)
        self._require_single_link(path_before)

        # On modern Windows/Python, pathname stat identity fields can be incomplete
        # even when fstat() has a usable file identity. Never compare those two stat
        # domains directly. A second read-only descriptor proves pathname -> handle
        # identity with sameopenfile(), while pathname metadata and handle metadata
        # are each checked only against snapshots from their own domain.
        try:
            with self.path.open("rb") as verification:
                verification_before = os.fstat(verification.fileno())
                same_file = os.path.sameopenfile(
                    handle.fileno(),
                    verification.fileno(),
                )
                path_after_open = os.stat(self.path, follow_symlinks=False)
                opened_after = os.fstat(handle.fileno())
                verification_after = os.fstat(verification.fileno())
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "workspace economic lock path changed during acquisition"
            ) from exc

        for snapshot in (
            verification_before,
            path_after_open,
            opened_after,
            verification_after,
        ):
            self._require_regular_file(snapshot)
            self._require_single_link(snapshot)

        if (
            not same_file
            or not self._stable_metadata(path_before, path_after_open)
            or not self._stable_metadata(opened_before, opened_after)
            or not self._stable_metadata(verification_before, verification_after)
        ):
            raise WorkspaceEconomicLockError(
                "workspace economic lock path changed during acquisition"
            )

    @staticmethod
    def _stable_metadata(left: os.stat_result, right: os.stat_result) -> bool:
        return (
            left.st_mode == right.st_mode
            and left.st_size == right.st_size
            and left.st_mtime_ns == right.st_mtime_ns
            and left.st_ctime_ns == right.st_ctime_ns
        )

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
