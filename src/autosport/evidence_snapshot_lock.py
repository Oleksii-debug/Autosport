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


class _WindowsWorkspaceChangeWatch:
    """Observe workspace changes across one evidence snapshot interval."""

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

        resolved = workspace.resolve(strict=True)
        self._resolved_workspace = resolved
        # ReadDirectoryChangesW reports changes within this directory but not a
        # rename/delete of the watched directory object itself. Keep DELETE sharing
        # disabled so the caller-visible workspace path cannot be renamed/replaced
        # away from this authoritative handle during the snapshot interval.
        directory_handle = create_file(
            str(resolved),
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

        # Retained canonical-source handles are released immediately before this
        # watcher is closed at context exit. Keep data/metadata notifications in
        # scope so a same-name in-place write cannot cross that handoff interval
        # silently after the retained handle has stopped denying WRITE.
        notify_filter = (
            file_notify_change_file_name
            | file_notify_change_dir_name
            | file_notify_change_attributes
            | file_notify_change_size
            | file_notify_change_last_write
            | file_notify_change_creation
            | file_notify_change_security
        )
        queued = read_changes(
            directory_handle,
            buffer,
            self._BUFFER_SIZE,
            False,
            notify_filter,
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
            self._resolved_workspace = None
            raise error

    def _create_linearization_sentinel(self) -> Path:
        workspace = self._resolved_workspace
        if workspace is None:
            raise RuntimeError("workspace change watch has no resolved root")

        # Keep the barrier name 8.3-compatible. FILE_NOTIFY_INFORMATION may
        # otherwise report either a long name or its short-name alias. Exclusive
        # creation means a collision fails closed rather than weakening the proof.
        sentinel = workspace / f"{uuid.uuid4().hex[:8]}.asv"
        descriptor = os.open(
            sentinel,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.close(descriptor)
        except BaseException:
            try:
                sentinel.unlink()
            except BaseException:
                pass
            raise
        return sentinel

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

    def close_and_require_unchanged(self) -> None:
        """Linearize the Windows watcher and fail if an earlier change was observed."""

        if self._closed:
            return
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
        sentinel: Path | None = None
        request_drained = False
        primary_error: BaseException | None = None
        try:
            # The previous implementation used CancelIoEx(ERROR_OPERATION_ABORTED)
            # as negative evidence. Windows documents cancellation only as the I/O
            # request's terminal status, not as proof that no directory change raced
            # the cancellation. Instead create one unique watched entry. Its creation
            # is the snapshot linearization point and must complete the already-pending
            # ReadDirectoryChangesW request normally.
            sentinel = self._create_linearization_sentinel()

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
                # Cleanup only. Cancellation status is deliberately never used as
                # acceptance evidence for the snapshot.
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
                    if (
                        drain_error != error_operation_aborted
                        and cleanup_error is None
                    ):
                        cleanup_error = ctypes.WinError(drain_error)

            if sentinel is not None:
                try:
                    sentinel.unlink()
                except FileNotFoundError:
                    pass
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error

            if not close_handle(event_handle) and cleanup_error is None:
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
            if not close_handle(directory_handle) and cleanup_error is None:
                cleanup_error = ctypes.WinError(ctypes.get_last_error())

            self._event_handle = None
            self._directory_handle = None
            self._buffer = None
            self._overlapped = None
            self._resolved_workspace = None

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
    mechanism. This wrapper is imported only by evidence export/verification so
    non-cooperating direct filesystem changes become observable without creating a
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
                    "workspace also changed during the evidence snapshot",
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
