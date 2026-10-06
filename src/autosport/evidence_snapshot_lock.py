from __future__ import annotations

import os
import uuid
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


def _parse_windows_directory_changes(payload: bytes) -> tuple[tuple[int, str], ...]:
    """Parse one ReadDirectoryChangesW FILE_NOTIFY_INFORMATION buffer."""

    records: list[tuple[int, str]] = []
    offset = 0
    payload_length = len(payload)
    while True:
        if payload_length - offset < 12:
            raise ValueError("malformed Windows directory-change notification buffer")

        next_entry_offset = int.from_bytes(
            payload[offset : offset + 4],
            "little",
            signed=False,
        )
        action = int.from_bytes(
            payload[offset + 4 : offset + 8],
            "little",
            signed=False,
        )
        name_length = int.from_bytes(
            payload[offset + 8 : offset + 12],
            "little",
            signed=False,
        )
        if name_length % 2:
            raise ValueError("malformed Windows directory-change filename length")

        name_start = offset + 12
        name_end = name_start + name_length
        if name_end > payload_length:
            raise ValueError("truncated Windows directory-change notification")
        try:
            name = payload[name_start:name_end].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid Windows directory-change filename") from exc
        records.append((action, name))

        if next_entry_offset == 0:
            return tuple(records)
        if next_entry_offset < 12 + name_length:
            raise ValueError("invalid Windows directory-change record offset")
        next_offset = offset + next_entry_offset
        if next_offset <= offset or next_offset >= payload_length:
            raise ValueError("invalid Windows directory-change record chain")
        offset = next_offset


def _windows_api_path(path: Path) -> str:
    text = os.path.abspath(str(path))
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _windows_final_path_from_handle(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path_name = kernel32.GetFinalPathNameByHandleW
    get_final_path_name.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path_name.restype = wintypes.DWORD

    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final_path_name(handle, buffer, len(buffer), 0)
    if length == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if length >= len(buffer):
        raise OSError("Windows evidence workspace path is too long")

    text = buffer.value
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[len("\\\\?\\UNC\\") :]
    elif text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\") :]
    return Path(text)


class _WindowsWorkspaceChangeWatch:
    """Pin and observe one Windows workspace across an evidence snapshot."""

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
        self._resolved_workspace: Path | None = None
        self._notify_filter: int | None = None
        self._armed = False
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
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
        file_flag_overlapped = 0x40000000
        file_notify_change_file_name = 0x00000001
        file_notify_change_dir_name = 0x00000002
        file_notify_change_attributes = 0x00000004
        file_notify_change_size = 0x00000008
        file_notify_change_last_write = 0x00000010
        file_notify_change_creation = 0x00000040
        file_notify_change_security = 0x00000100
        invalid_handle_value = ctypes.c_void_p(-1).value

        # Open the caller-selected path itself first. Normal reparse processing is
        # intentionally retained so a symlink/junction binds its target object. The
        # resulting directory handle omits FILE_SHARE_DELETE, so that bound object
        # cannot be renamed/deleted/replaced while we derive and use its final path.
        directory_handle = create_file(
            _windows_api_path(workspace),
            file_list_directory,
            file_share_read | file_share_write,
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_overlapped,
            None,
        )
        if directory_handle == invalid_handle_value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._directory_handle = int(directory_handle)

        try:
            self._resolved_workspace = _windows_final_path_from_handle(
                self._directory_handle
            )
        except BaseException:
            close_handle(directory_handle)
            self._directory_handle = None
            raise

        event_handle = create_event(None, True, False, None)
        if not event_handle:
            error = ctypes.WinError(ctypes.get_last_error())
            close_handle(directory_handle)
            self._directory_handle = None
            self._resolved_workspace = None
            raise error
        self._event_handle = int(event_handle)

        buffer = (wintypes.DWORD * (self._BUFFER_SIZE // 4))()
        overlapped = Overlapped()
        overlapped.hEvent = event_handle
        self._buffer = buffer
        self._overlapped = overlapped
        self._notify_filter = (
            file_notify_change_file_name
            | file_notify_change_dir_name
            | file_notify_change_attributes
            | file_notify_change_size
            | file_notify_change_last_write
            | file_notify_change_creation
            | file_notify_change_security
        )

    @property
    def resolved_workspace(self) -> Path:
        workspace = self._resolved_workspace
        if workspace is None:
            raise RuntimeError("workspace change watch has no resolved root")
        return workspace

    def arm(self) -> None:
        """Start observation after the canonical advisory lock is acquired."""

        if self._closed:
            raise RuntimeError("workspace change watch is already closed")
        if self._armed:
            return
        directory_handle = self._directory_handle
        buffer = self._buffer
        overlapped = self._overlapped
        notify_filter = self._notify_filter
        if (
            directory_handle is None
            or buffer is None
            or overlapped is None
            or notify_filter is None
        ):
            raise RuntimeError("workspace change watch is not fully initialized")

        queued = self._kernel32.ReadDirectoryChangesW(
            directory_handle,
            buffer,
            self._BUFFER_SIZE,
            False,
            notify_filter,
            None,
            self._ctypes.byref(overlapped),
            None,
        )
        if not queued:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        self._armed = True

    def _create_linearization_sentinel(self) -> tuple[Path, int]:
        workspace = self.resolved_workspace

        ctypes = self._ctypes
        wintypes = self._wintypes
        create_file = self._kernel32.CreateFileW
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

        generic_write = 0x40000000
        delete_access = 0x00010000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        create_new = 1
        file_attribute_temporary = 0x00000100
        file_flag_delete_on_close = 0x04000000
        invalid_handle_value = ctypes.c_void_p(-1).value

        # Keep the barrier name 8.3-compatible. FILE_NOTIFY_INFORMATION may
        # otherwise report either a long name or its short-name alias. The native
        # handle remains authoritative until cleanup: DELETE access plus
        # DELETE_ON_CLOSE owns deletion of this exact object, and omitting
        # FILE_SHARE_DELETE prevents a foreign rename/replacement from substituting
        # another object at the pathname before cleanup.
        sentinel = workspace / f"{uuid.uuid4().hex[:8]}.asv"
        sentinel_handle = create_file(
            str(sentinel),
            generic_write | delete_access,
            file_share_read | file_share_write,
            None,
            create_new,
            file_attribute_temporary | file_flag_delete_on_close,
            None,
        )
        if sentinel_handle == invalid_handle_value:
            raise ctypes.WinError(ctypes.get_last_error())
        return sentinel, int(sentinel_handle)

    @staticmethod
    def _require_only_sentinel_notifications(
        records: tuple[tuple[int, str], ...],
        sentinel_name: str,
    ) -> None:
        file_action_added = 0x00000001
        file_action_modified = 0x00000003
        expected_name = sentinel_name.casefold()
        saw_added = False

        for action, name in records:
            if name.casefold() != expected_name:
                raise ValueError(
                    "workspace changed during evidence snapshot namespace boundary"
                )
            if action == file_action_added:
                saw_added = True
                continue
            if action == file_action_modified:
                continue
            raise ValueError(
                "unexpected sentinel notification before evidence snapshot linearization"
            )

        if not saw_added:
            raise ValueError(
                "evidence snapshot sentinel creation notification was not observed"
            )

    def close_without_validation(self) -> None:
        """Cleanup the pin/watch without treating cancellation as acceptance evidence."""

        if self._closed:
            return
        self._closed = True

        ctypes = self._ctypes
        wintypes = self._wintypes
        directory_handle = self._directory_handle
        event_handle = self._event_handle
        overlapped = self._overlapped
        cleanup_error: BaseException | None = None

        if self._armed and directory_handle is not None and overlapped is not None:
            get_result = self._kernel32.GetOverlappedResult
            get_result.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(self._overlapped_type),
                ctypes.POINTER(wintypes.DWORD),
                wintypes.BOOL,
            )
            get_result.restype = wintypes.BOOL
            cancel_io = self._kernel32.CancelIoEx
            cancel_io.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(self._overlapped_type),
            )
            cancel_io.restype = wintypes.BOOL

            error_not_found = 1168
            if not cancel_io(directory_handle, ctypes.byref(overlapped)):
                cancel_error = ctypes.get_last_error()
                if cancel_error != error_not_found:
                    cleanup_error = ctypes.WinError(cancel_error)
            transferred = wintypes.DWORD()
            drained = get_result(
                directory_handle,
                ctypes.byref(overlapped),
                ctypes.byref(transferred),
                True,
            )
            if not drained:
                drain_error = ctypes.get_last_error()
                error_operation_aborted = 995
                if drain_error != error_operation_aborted and cleanup_error is None:
                    cleanup_error = ctypes.WinError(drain_error)

        close_handle = self._kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        if event_handle is not None and not close_handle(event_handle) and cleanup_error is None:
            cleanup_error = ctypes.WinError(ctypes.get_last_error())
        if directory_handle is not None and not close_handle(directory_handle) and cleanup_error is None:
            cleanup_error = ctypes.WinError(ctypes.get_last_error())

        self._event_handle = None
        self._directory_handle = None
        self._buffer = None
        self._overlapped = None
        self._resolved_workspace = None
        self._notify_filter = None
        self._armed = False

        if cleanup_error is not None:
            raise cleanup_error

    def close_and_require_unchanged(self) -> None:
        """Linearize the Windows watcher and fail if an earlier change was observed."""

        if self._closed:
            return
        if not self._armed:
            raise RuntimeError("workspace change watch was not armed before linearization")
        self._closed = True

        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32
        directory_handle = self._directory_handle
        event_handle = self._event_handle
        buffer = self._buffer
        overlapped = self._overlapped
        if (
            directory_handle is None
            or event_handle is None
            or buffer is None
            or overlapped is None
        ):
            raise RuntimeError("workspace change watch is not fully initialized")

        get_result = kernel32.GetOverlappedResult
        get_result.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(self._overlapped_type),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        )
        get_result.restype = wintypes.BOOL

        cancel_io = kernel32.CancelIoEx
        cancel_io.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(self._overlapped_type),
        )
        cancel_io.restype = wintypes.BOOL

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        error_not_found = 1168
        sentinel_handle: int | None = None
        request_drained = False
        primary_error: BaseException | None = None
        try:
            # Create one unique watched entry. Its creation is the positive snapshot
            # linearization point and must complete the already-pending request
            # normally; cancellation remains cleanup-only.
            sentinel, sentinel_handle = self._create_linearization_sentinel()

            transferred = wintypes.DWORD()
            completed = get_result(
                directory_handle,
                ctypes.byref(overlapped),
                ctypes.byref(transferred),
                True,
            )
            request_drained = True
            if not completed:
                raise ctypes.WinError(ctypes.get_last_error())
            if transferred.value == 0:
                raise ValueError(
                    "workspace change notification overflowed before evidence snapshot linearization"
                )
            if transferred.value > self._BUFFER_SIZE:
                raise ValueError(
                    "workspace change notification exceeded the evidence snapshot buffer"
                )

            payload = ctypes.string_at(ctypes.addressof(buffer), transferred.value)
            records = _parse_windows_directory_changes(payload)
            self._require_only_sentinel_notifications(records, sentinel.name)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None

            if not request_drained:
                if not cancel_io(directory_handle, ctypes.byref(overlapped)):
                    cancel_error = ctypes.get_last_error()
                    if cancel_error != error_not_found:
                        cleanup_error = ctypes.WinError(cancel_error)
                transferred = wintypes.DWORD()
                drained = get_result(
                    directory_handle,
                    ctypes.byref(overlapped),
                    ctypes.byref(transferred),
                    True,
                )
                if not drained:
                    drain_error = ctypes.get_last_error()
                    error_operation_aborted = 995
                    if drain_error != error_operation_aborted and cleanup_error is None:
                        cleanup_error = ctypes.WinError(drain_error)

            # Closing the retained native sentinel handle deletes exactly the
            # object we created. No pathname unlink is used.
            if sentinel_handle is not None:
                if not close_handle(sentinel_handle) and cleanup_error is None:
                    cleanup_error = ctypes.WinError(ctypes.get_last_error())

            if not close_handle(event_handle) and cleanup_error is None:
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
            if not close_handle(directory_handle) and cleanup_error is None:
                cleanup_error = ctypes.WinError(ctypes.get_last_error())

            self._event_handle = None
            self._directory_handle = None
            self._buffer = None
            self._overlapped = None
            self._resolved_workspace = None
            self._notify_filter = None
            self._armed = False

            if primary_error is not None and cleanup_error is not None:
                _add_secondary_failure_note(
                    primary_error,
                    "workspace snapshot watcher cleanup also failed",
                    cleanup_error,
                )
            elif primary_error is None and cleanup_error is not None:
                raise cleanup_error


class WorkspaceEconomicLock:
    """Evidence-only wrapper adding a Windows direct-filesystem snapshot boundary.

    The canonical WorkspaceEconomicLock remains the advisory writer-coordination
    mechanism. On Windows this wrapper first pins the caller-selected directory
    object without DELETE sharing, derives the authoritative final path from that
    handle, then acquires the canonical lock and arms ReadDirectoryChangesW against
    the same retained object. Retained source handles are expected to remain alive
    through explicit ``linearize()`` before source cleanup.
    """

    def __init__(self, workspace: str | Path) -> None:
        requested_workspace = Path(workspace)
        self._watch: _WindowsWorkspaceChangeWatch | None = None
        self._entered = False
        self._linearized = False

        if os.name == "nt":
            self._watch = _WindowsWorkspaceChangeWatch(requested_workspace)
            self.workspace = self._watch.resolved_workspace
        else:
            # Preserve the predecessor evidence boundary: POSIX callers operate on
            # one strict-resolved workspace target rather than a retargetable alias.
            self.workspace = requested_workspace.resolve(strict=True)

        try:
            self._lock = _WorkspaceEconomicLock(self.workspace)
        except BaseException as lock_error:
            if self._watch is not None:
                try:
                    self._watch.close_without_validation()
                except BaseException as cleanup_error:
                    _add_secondary_failure_note(
                        lock_error,
                        "workspace object pin cleanup also failed",
                        cleanup_error,
                    )
                self._watch = None
            raise

    def close(self) -> None:
        """Cleanup an unentered/preflight Windows root pin without accepting a snapshot."""

        if self._entered:
            raise RuntimeError("cannot cleanup WorkspaceEconomicLock while it is entered")
        if self._watch is not None:
            watch = self._watch
            self._watch = None
            watch.close_without_validation()

    def __enter__(self) -> "WorkspaceEconomicLock":
        if self._entered:
            raise RuntimeError("WorkspaceEconomicLock is already entered")
        self._lock.acquire()
        try:
            if self._watch is not None:
                self._watch.arm()
        except BaseException as watch_error:
            try:
                self._lock.release()
            except BaseException as release_error:
                _add_secondary_failure_note(
                    watch_error,
                    "WorkspaceEconomicLock release also failed after namespace watch arm failure",
                    release_error,
                )
            if self._watch is not None:
                watch = self._watch
                self._watch = None
                try:
                    watch.close_without_validation()
                except BaseException as cleanup_error:
                    _add_secondary_failure_note(
                        watch_error,
                        "workspace object pin cleanup also failed after watch arm failure",
                        cleanup_error,
                    )
            raise
        self._entered = True
        return self

    def linearize(self) -> None:
        """Close the positive Windows watcher boundary while callers still hold sources."""

        if not self._entered:
            raise RuntimeError("WorkspaceEconomicLock must be entered before linearization")
        if self._linearized:
            return
        if self._watch is not None:
            watch = self._watch
            try:
                watch.close_and_require_unchanged()
            finally:
                self._watch = None
        self._linearized = True

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self._entered:
            return None

        try:
            if exc_value is not None:
                watch_error: BaseException | None = None
                if self._watch is not None:
                    watch = self._watch
                    self._watch = None
                    try:
                        watch.close_without_validation()
                    except BaseException as error:
                        watch_error = error
                if watch_error is not None:
                    _add_secondary_failure_note(
                        exc_value,
                        "workspace snapshot cleanup also failed",
                        watch_error,
                    )
                return self._lock.__exit__(exc_type, exc_value, traceback)

            # Backward-compatible direct wrapper use still receives a positive
            # boundary. Evidence export/verify explicitly call linearize() before
            # retained-source cleanup, so their context teardown is cleanup-only.
            try:
                self.linearize()
            except BaseException as watch_error:
                try:
                    self._lock.release()
                except BaseException as release_error:
                    _add_secondary_failure_note(
                        watch_error,
                        "WorkspaceEconomicLock release also failed after namespace boundary failure",
                        release_error,
                    )
                raise

            return self._lock.__exit__(None, None, None)
        finally:
            self._entered = False
