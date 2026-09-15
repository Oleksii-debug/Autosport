from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .workspace_lock import (
    WorkspaceEconomicLock as _WorkspaceEconomicLock,
    WorkspaceEconomicLockError,
)


def _add_secondary_failure_note(
    primary: BaseException,
    prefix: str,
    secondary: BaseException,
) -> None:
    try:
        primary.add_note(f"{prefix}: {type(secondary).__name__}: {secondary}")
    except BaseException:
        return


class _WindowsWorkspaceChangeWatch:
    """Observe workspace namespace changes across one evidence snapshot interval."""

    _BUFFER_SIZE = 64 * 1024

    def __init__(self, workspace: Path) -> None:
        import ctypes
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._overlapped_type = Overlapped
        self._directory_handle: int | None = None
        self._event_handle: int | None = None
        self._buffer: Any | None = None
        self._overlapped: Any | None = None
        self._closed = False

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32

        create_file = kernel32.CreateFileW
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

        create_event = kernel32.CreateEventW
        create_event.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        create_event.restype = wintypes.HANDLE

        read_changes = kernel32.ReadDirectoryChangesW
        read_changes.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(Overlapped),
            ctypes.c_void_p,
        )
        read_changes.restype = wintypes.BOOL

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        file_list_directory = 0x00000001
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
        file_flag_overlapped = 0x40000000
        file_notify_change_file_name = 0x00000001
        file_notify_change_dir_name = 0x00000002
        invalid_handle_value = ctypes.c_void_p(-1).value

        resolved = workspace.resolve(strict=True)
        directory_handle = create_file(
            str(resolved),
            file_list_directory,
            file_share_read | file_share_write | file_share_delete,
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_overlapped,
            None,
        )
        if directory_handle == invalid_handle_value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._directory_handle = int(directory_handle)

        event_handle = create_event(None, True, False, None)
        if not event_handle:
            error = ctypes.WinError(ctypes.get_last_error())
            close_handle(directory_handle)
            self._directory_handle = None
            raise error
        self._event_handle = int(event_handle)

        buffer = (wintypes.DWORD * (self._BUFFER_SIZE // 4))()
        overlapped = Overlapped()
        overlapped.hEvent = event_handle
        self._buffer = buffer
        self._overlapped = overlapped

        queued = read_changes(
            directory_handle,
            buffer,
            self._BUFFER_SIZE,
            False,
            file_notify_change_file_name | file_notify_change_dir_name,
            None,
            ctypes.byref(overlapped),
            None,
        )
        if not queued:
            error = ctypes.WinError(ctypes.get_last_error())
            close_handle(event_handle)
            close_handle(directory_handle)
            self._event_handle = None
            self._directory_handle = None
            self._buffer = None
            self._overlapped = None
            raise error

    def close_and_require_unchanged(self) -> None:
        """Linearize the Windows namespace watch and fail if it saw a change."""

        if self._closed:
            return
        self._closed = True

        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32
        directory_handle = self._directory_handle
        event_handle = self._event_handle
        overlapped = self._overlapped
        if directory_handle is None or event_handle is None or overlapped is None:
            raise RuntimeError("workspace namespace watch is not fully initialized")

        cancel_io = kernel32.CancelIoEx
        cancel_io.argtypes = (wintypes.HANDLE, ctypes.POINTER(self._overlapped_type))
        cancel_io.restype = wintypes.BOOL
        get_result = kernel32.GetOverlappedResult
        get_result.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(self._overlapped_type),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        )
        get_result.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        error_not_found = 1168
        error_operation_aborted = 995
        primary_error: BaseException | None = None
        try:
            if not cancel_io(directory_handle, ctypes.byref(overlapped)):
                cancel_error = ctypes.get_last_error()
                if cancel_error != error_not_found:
                    raise ctypes.WinError(cancel_error)

            transferred = wintypes.DWORD()
            completed = get_result(
                directory_handle,
                ctypes.byref(overlapped),
                ctypes.byref(transferred),
                True,
            )
            if completed:
                raise ValueError(
                    "workspace changed during evidence snapshot namespace boundary"
                )

            completion_error = ctypes.get_last_error()
            if completion_error != error_operation_aborted:
                raise ctypes.WinError(completion_error)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            if not close_handle(event_handle):
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
            if not close_handle(directory_handle) and cleanup_error is None:
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
            self._event_handle = None
            self._directory_handle = None
            self._buffer = None
            self._overlapped = None
            if primary_error is not None and cleanup_error is not None:
                _add_secondary_failure_note(
                    primary_error,
                    "workspace namespace watch cleanup also failed",
                    cleanup_error,
                )
            elif primary_error is None and cleanup_error is not None:
                raise cleanup_error


class WorkspaceEconomicLock:
    """Evidence-only wrapper adding a Windows direct-filesystem namespace boundary.

    The canonical WorkspaceEconomicLock remains the advisory writer-coordination
    mechanism. This wrapper is imported only by evidence export/verification so
    non-cooperating direct namespace changes become observable without creating a
    second lock/state architecture. Retained source handles in evidence_export keep
    canonical member bytes/path identities stable through the final source reproof.
    """

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self._lock = _WorkspaceEconomicLock(self.workspace)
        self._watch: _WindowsWorkspaceChangeWatch | None = None

    def __enter__(self) -> "WorkspaceEconomicLock":
        self._lock.acquire()
        try:
            if os.name == "nt":
                self._watch = _WindowsWorkspaceChangeWatch(self.workspace)
        except BaseException as watch_error:
            try:
                self._lock.release()
            except BaseException as release_error:
                _add_secondary_failure_note(
                    watch_error,
                    "WorkspaceEconomicLock release also failed after namespace watch acquisition failure",
                    release_error,
                )
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        watch_error: BaseException | None = None
        if self._watch is not None:
            try:
                self._watch.close_and_require_unchanged()
            except BaseException as error:
                watch_error = error
            finally:
                self._watch = None

        if exc_value is not None:
            if watch_error is not None:
                _add_secondary_failure_note(
                    exc_value,
                    "workspace namespace also changed during the evidence snapshot",
                    watch_error,
                )
            return self._lock.__exit__(exc_type, exc_value, traceback)

        if watch_error is not None:
            try:
                self._lock.release()
            except BaseException as release_error:
                _add_secondary_failure_note(
                    watch_error,
                    "WorkspaceEconomicLock release also failed after namespace boundary failure",
                    release_error,
                )
            raise watch_error

        return self._lock.__exit__(None, None, None)
