from __future__ import annotations

import ctypes
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
from ctypes import wintypes
from types import ModuleType
from typing import Any


_OWNER_RIGHTS_SID = "S-1-3-4"
_FILE_WRITE_DATA = 0x00000002
_FILE_APPEND_DATA = 0x00000004
_FILE_WRITE_EA = 0x00000010
_FILE_DELETE_CHILD = 0x00000040
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_WRITE_ATTRIBUTES = 0x00000100
_DELETE_ACCESS = 0x00010000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_GENERIC_ALL = 0x10000000
_GENERIC_WRITE = 0x40000000
_MUTATION_CAPABLE_ACCESS = (
    _FILE_WRITE_DATA
    | _FILE_APPEND_DATA
    | _FILE_WRITE_EA
    | _FILE_WRITE_ATTRIBUTES
    | _DELETE_ACCESS
    | _WRITE_DAC
    | _WRITE_OWNER
    | _GENERIC_ALL
    | _GENERIC_WRITE
)
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_FILE_OBJECT = 1
_SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_MAX_SYSTEM_HANDLE_SNAPSHOT_BYTES = 64 * 1024 * 1024
_TEST_DACL_REWRITE_ENV = "AUTOSPORT_TEST_REWRITE_EXPECTED_DACL_AFTER_RESOURCE_END"
_FILE_ID_INFO_CLASS = 18
_FILE_PROCESS_IDS_USING_FILE_INFORMATION = 47
_TARGET_PROCESS_IDS_BUFFER_BYTES = 64 * 1024
_PROCESS_DUP_HANDLE = 0x00000040
_DUPLICATE_SAME_ACCESS = 0x00000002
_MAX_UNINSPECTABLE_HANDLE_RESCANS = 4


class _SystemHandleTableEntryInfoEx(ctypes.Structure):
    _fields_ = (
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_size_t),
        ("HandleValue", ctypes.c_size_t),
        ("GrantedAccess", ctypes.c_uint32),
        ("CreatorBackTraceIndex", ctypes.c_uint16),
        ("ObjectTypeIndex", ctypes.c_uint16),
        ("HandleAttributes", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32),
    )


class _FileId128(ctypes.Structure):
    _fields_ = (("ByteIdentifier", ctypes.c_ubyte * 16),)


class _FileIdInfo(ctypes.Structure):
    _fields_ = (
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", _FileId128),
    )


class _IoStatusBlock(ctypes.Structure):
    _fields_ = (
        ("StatusOrPointer", ctypes.c_void_p),
        ("Information", ctypes.c_size_t),
    )


class _WtsProcessInfoW(ctypes.Structure):
    _fields_ = (
        ("SessionId", wintypes.DWORD),
        ("ProcessId", wintypes.DWORD),
        ("pProcessName", wintypes.LPWSTR),
        ("pUserSid", ctypes.c_void_p),
    )


class _SystemHandleSnapshot(list[tuple[int, int, int, int]]):
    def __init__(self) -> None:
        super().__init__()
        self.object_types: dict[tuple[int, int, int], int] = {}


class _CandidateFileIdentityUnavailable(RuntimeError):
    """A snapshotted candidate is not currently inspectable by stable file identity."""


def _raw_handle_value(raw_handle: Any) -> int:
    value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    if value is None:
        raise RuntimeError("trusted expected-snapshot security handle has no value")
    return int(value)


def _file_identity(raw_handle: Any) -> tuple[int, bytes]:
    """Return stable volume/file identity for an inspectable Windows file handle."""

    if os.name != "nt":
        raise RuntimeError("file identity audit requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    get_info.restype = wintypes.BOOL
    info = _FileIdInfo()
    ctypes.set_last_error(0)
    if not get_info(
        raw_handle,
        _FILE_ID_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.VolumeSerialNumber), bytes(info.FileId.ByteIdentifier)


def _candidate_file_identity(pid: int, handle_value: int) -> tuple[int, bytes]:
    """Inspect a snapshotted handle without trusting its FILE_OBJECT pointer.

    Inspection failure is not evidence that the candidate disappeared. The caller
    revalidates the exact handle-table row and only ignores a positively vanished row.
    """

    current_pid = os.getpid()
    if pid == current_pid:
        try:
            return _file_identity(handle_value)
        except OSError as exc:
            raise _CandidateFileIdentityUnavailable(
                "current-process candidate FileIdInfo is unavailable"
            ) from exc

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    duplicate_handle = kernel32.DuplicateHandle
    duplicate_handle.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    duplicate_handle.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    process = open_process(_PROCESS_DUP_HANDLE, False, pid)
    process_value = (
        process if isinstance(process, int) else ctypes.cast(process, ctypes.c_void_p).value
    )
    if not process_value:
        error = ctypes.get_last_error()
        raise _CandidateFileIdentityUnavailable(
            f"candidate process cannot be opened for handle duplication: WinError {error}"
        )
    duplicated = wintypes.HANDLE()
    try:
        ctypes.set_last_error(0)
        if not duplicate_handle(
            process,
            wintypes.HANDLE(handle_value),
            get_current_process(),
            ctypes.byref(duplicated),
            0,
            False,
            _DUPLICATE_SAME_ACCESS,
        ):
            error = ctypes.get_last_error()
            raise _CandidateFileIdentityUnavailable(
                f"candidate handle cannot be duplicated: WinError {error}"
            )
        try:
            return _file_identity(duplicated)
        except OSError as exc:
            raise _CandidateFileIdentityUnavailable(
                "candidate FileIdInfo is unavailable"
            ) from exc
        finally:
            close_handle(duplicated)
    finally:
        close_handle(process)


def _target_process_ids_using_file(raw_handle: Any) -> set[int]:
    """Return the exact PIDs Windows reports as using the retained target file object."""

    if os.name != "nt":
        raise RuntimeError("target process-id oracle requires Windows")

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    query = ntdll.NtQueryInformationFile
    query.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
    )
    query.restype = ctypes.c_int32

    buffer = ctypes.create_string_buffer(_TARGET_PROCESS_IDS_BUFFER_BYTES)
    io_status = _IoStatusBlock()
    status = int(
        query(
            raw_handle,
            ctypes.byref(io_status),
            ctypes.cast(buffer, ctypes.c_void_p),
            len(buffer),
            _FILE_PROCESS_IDS_USING_FILE_INFORMATION,
        )
    )
    status_u32 = ctypes.c_uint32(status).value
    if status_u32 != 0:
        raise RuntimeError(
            "target process-id oracle failed: "
            f"NTSTATUS=0x{status_u32:08x}"
        )

    returned = int(io_status.Information)
    pointer_size = ctypes.sizeof(ctypes.c_size_t)
    count_size = ctypes.sizeof(ctypes.c_ulong)
    ids_offset = ((count_size + pointer_size - 1) // pointer_size) * pointer_size
    if returned < ids_offset or returned > len(buffer):
        raise RuntimeError("target process-id oracle response was truncated")
    count = int(ctypes.c_ulong.from_buffer_copy(buffer.raw[:count_size]).value)
    required = ids_offset + count * pointer_size
    if required > returned or required > len(buffer):
        raise RuntimeError("target process-id oracle response length was inconsistent")

    process_ids: set[int] = set()
    for index in range(count):
        offset = ids_offset + index * pointer_size
        pid = int(
            ctypes.c_size_t.from_buffer_copy(
                buffer.raw[offset : offset + pointer_size]
            ).value
        )
        if pid <= 0 or pid > 0xFFFFFFFF:
            raise RuntimeError("target process-id oracle returned an invalid PID")
        process_ids.add(pid)

    current_pid = os.getpid()
    if current_pid not in process_ids:
        raise RuntimeError(
            "target process-id oracle did not enumerate trusted current-process handle"
        )
    return process_ids


def _process_same_user_scope() -> dict[int, bool]:
    """Classify active Windows PIDs against the current user's SID without opening them.

    This is used only for same-type candidates whose stable file identity cannot be
    inspected. A missing/unknown owner remains fail-closed. A positively different-user
    process is not sufficient target disassociation: it still requires the retained
    target's process-id oracle to prove that PID is not using the protected file object.
    """

    if os.name != "nt":
        raise RuntimeError("process user-scope classification requires Windows")

    wtsapi32 = ctypes.WinDLL("Wtsapi32", use_last_error=True)
    enumerate_processes = wtsapi32.WTSEnumerateProcessesW
    enumerate_processes.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_WtsProcessInfoW)),
        ctypes.POINTER(wintypes.DWORD),
    )
    enumerate_processes.restype = wintypes.BOOL
    free_memory = wtsapi32.WTSFreeMemory
    free_memory.argtypes = (ctypes.c_void_p,)
    free_memory.restype = None

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    equal_sid = advapi32.EqualSid
    equal_sid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    equal_sid.restype = wintypes.BOOL

    processes = ctypes.POINTER(_WtsProcessInfoW)()
    count = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    if not enumerate_processes(
        None,
        0,
        1,
        ctypes.byref(processes),
        ctypes.byref(count),
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        current_pid = os.getpid()
        current_sid: int | None = None
        for index in range(int(count.value)):
            info = processes[index]
            if int(info.ProcessId) == current_pid:
                current_sid = int(info.pUserSid or 0)
                break
        if not current_sid:
            raise RuntimeError(
                "current process user SID is unavailable from Windows process enumeration"
            )

        scope: dict[int, bool] = {}
        for index in range(int(count.value)):
            info = processes[index]
            pid = int(info.ProcessId)
            sid = int(info.pUserSid or 0)
            if not sid:
                continue
            scope[pid] = bool(equal_sid(ctypes.c_void_p(sid), ctypes.c_void_p(current_sid)))
        if scope.get(current_pid) is not True:
            raise RuntimeError("current process user SID classification was not self-consistent")
        return scope
    finally:
        if processes:
            free_memory(ctypes.cast(processes, ctypes.c_void_p))


def _query_system_handles() -> list[tuple[int, int, int, int]]:
    """Capture the Windows extended handle table or fail closed."""

    if os.name != "nt":
        raise RuntimeError("system handle authority audit requires Windows")

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    query = ntdll.NtQuerySystemInformation
    query.argtypes = (
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    )
    query.restype = ctypes.c_int32

    size = 1024 * 1024
    while size <= _MAX_SYSTEM_HANDLE_SNAPSHOT_BYTES:
        buffer = ctypes.create_string_buffer(size)
        returned = ctypes.c_uint32(0)
        status = int(
            query(
                _SYSTEM_EXTENDED_HANDLE_INFORMATION,
                ctypes.cast(buffer, ctypes.c_void_p),
                size,
                ctypes.byref(returned),
            )
        )
        status_u32 = ctypes.c_uint32(status).value
        if status_u32 == 0:
            header_size = ctypes.sizeof(ctypes.c_size_t) * 2
            if size < header_size:
                raise RuntimeError("system handle authority snapshot header was truncated")
            count = ctypes.c_size_t.from_buffer_copy(buffer.raw[: ctypes.sizeof(ctypes.c_size_t)]).value
            entry_size = ctypes.sizeof(_SystemHandleTableEntryInfoEx)
            required = header_size + int(count) * entry_size
            if required > size:
                raise RuntimeError("system handle authority snapshot entries were truncated")
            snapshot = _SystemHandleSnapshot()
            for index in range(int(count)):
                offset = header_size + index * entry_size
                entry = _SystemHandleTableEntryInfoEx.from_buffer_copy(
                    buffer.raw[offset : offset + entry_size]
                )
                row = (
                    int(entry.Object or 0),
                    int(entry.UniqueProcessId),
                    int(entry.HandleValue),
                    int(entry.GrantedAccess),
                )
                snapshot.append(row)
                snapshot.object_types[(row[0], row[1], row[2])] = int(entry.ObjectTypeIndex)
            return snapshot
        if status_u32 != _STATUS_INFO_LENGTH_MISMATCH:
            raise RuntimeError(
                "system handle authority snapshot failed: "
                f"NTSTATUS=0x{status_u32:08x}"
            )
        requested = int(returned.value)
        size = max(size * 2, requested + 64 * 1024)

    raise RuntimeError("system handle authority snapshot exceeded bounded capture size")


def _snapshot_row_still_present(
    row: tuple[int, int, int, int],
    *,
    object_type: int,
) -> bool:
    """Revalidate an uninspectable candidate by exact handle-table identity."""

    refreshed = _query_system_handles()
    if row not in refreshed:
        return False
    object_id, pid, handle_value, _granted_access = row
    refreshed_types = getattr(refreshed, "object_types", {})
    return refreshed_types.get((object_id, pid, handle_value)) == object_type


def _require_no_competing_mutation_handles(
    raw_handle: Any,
    *,
    label: str,
    directory: bool,
    allow_current_process_data_mutators: bool = False,
    _uninspectable_handle_rescans_remaining: int = _MAX_UNINSPECTABLE_HANDLE_RESCANS,
) -> None:
    """Reject retained mutation authority that predates the filesystem deny fences.

    DACL denies prevent fresh opens, but they do not revoke access already granted to
    a live handle. Bind the trusted security-authority handle to stable filesystem
    identity and reject every other mutation-capable handle. The kernel-object pointer
    remains a fast path, but separate CreateFile opens are compared by FileIdInfo.
    Uninspectable same-type candidates are deferred until the full snapshot has been
    scanned so a proven same-kernel-object competitor cannot be masked by unrelated
    hosted-runner authority. A live uninspectable current-user candidate fails closed;
    a different-user candidate is ignored only after the retained target's process-id
    oracle positively disassociates its PID and the exact handle-table row remains live.
    Unknown owner/oracle states and vanished-row churn fail closed or bounded-rescan.
    Proven same-file competitors remain fatal regardless of user. During a native
    PyInstaller resource update, current-process data/delete handles are the trusted
    mutator and may remain; security-descriptor mutation authority is never exempted.
    FILE_DELETE_CHILD is directory-only: the same access bit is FILE_EXECUTE on regular
    files.
    """

    trusted_handle = _raw_handle_value(raw_handle)
    current_pid = os.getpid()
    snapshot = _query_system_handles()
    trusted_rows = [
        row
        for row in snapshot
        if row[1] == current_pid and row[2] == trusted_handle
    ]
    if len(trusted_rows) != 1:
        raise RuntimeError(f"{label} trusted handle was not uniquely present in system handle table")
    target_object, _pid, _handle, trusted_access = trusted_rows[0]
    if target_object == 0 or trusted_access & _WRITE_DAC == 0:
        raise RuntimeError(f"{label} trusted handle lost WRITE_DAC authority")
    object_types = getattr(snapshot, "object_types", {})
    target_type = object_types.get((target_object, current_pid, trusted_handle))
    target_identity: tuple[int, bytes] | None = None

    mutation_mask = _MUTATION_CAPABLE_ACCESS | (_FILE_DELETE_CHILD if directory else 0)
    competing: list[tuple[int, int, int, int]] = []
    uninspectable: list[
        tuple[
            tuple[int, int, int, int],
            int,
            _CandidateFileIdentityUnavailable,
        ]
    ] = []
    for row in snapshot:
        object_id, pid, handle_value, granted_access = row
        if pid == current_pid and handle_value == trusted_handle:
            continue
        if granted_access & mutation_mask == 0:
            continue
        if (
            allow_current_process_data_mutators
            and pid == current_pid
            and granted_access & (_WRITE_DAC | _WRITE_OWNER) == 0
        ):
            continue
        same_file = object_id == target_object
        if not same_file and target_type is not None:
            candidate_type = object_types.get((object_id, pid, handle_value))
            if candidate_type != target_type:
                continue
            if target_identity is None:
                target_identity = _file_identity(raw_handle)
            try:
                candidate_identity = _candidate_file_identity(pid, handle_value)
            except _CandidateFileIdentityUnavailable as exc:
                assert candidate_type is not None
                uninspectable.append((row, candidate_type, exc))
                continue
            same_file = candidate_identity == target_identity
        if same_file:
            competing.append(row)

    if competing:
        raise RuntimeError(
            f"{label} has {len(competing)} pre-existing competing mutation-capable handle(s)"
        )

    if uninspectable:
        process_scope: dict[int, bool] | None = None
        target_process_ids: set[int] | None = None
        needs_rescan = False
        last_exc = uninspectable[-1][2]
        for row, candidate_type, exc in uninspectable:
            if not _snapshot_row_still_present(row, object_type=candidate_type):
                needs_rescan = True
                last_exc = exc
                continue
            if process_scope is None:
                try:
                    process_scope = _process_same_user_scope()
                except Exception as scope_exc:
                    raise RuntimeError(
                        f"{label} has live uninspectable mutation-capable handle with unknown process owner"
                    ) from scope_exc
            pid = row[1]
            same_user = process_scope.get(pid)
            if same_user is None:
                raise RuntimeError(
                    f"{label} has live uninspectable mutation-capable handle with unknown process owner"
                ) from exc
            if same_user:
                raise RuntimeError(
                    f"{label} has live same-user uninspectable mutation-capable handle"
                ) from exc
            if target_process_ids is None:
                try:
                    target_process_ids = _target_process_ids_using_file(raw_handle)
                except Exception as oracle_exc:
                    raise RuntimeError(
                        f"{label} target process-id oracle is unavailable for live "
                        "uninspectable mutation-capable handle"
                    ) from oracle_exc
            if pid in target_process_ids:
                raise RuntimeError(
                    f"{label} has live different-user uninspectable mutation-capable "
                    "handle positively associated with target"
                ) from exc
            if not _snapshot_row_still_present(row, object_type=candidate_type):
                needs_rescan = True
                last_exc = exc
                continue
        if not needs_rescan:
            return
        if _uninspectable_handle_rescans_remaining <= 0:
            raise RuntimeError(
                f"{label} mutation-capable handle audit did not quiesce after "
                "uninspectable candidate churn"
            ) from last_exc
        return _require_no_competing_mutation_handles(
            raw_handle,
            label=label,
            directory=directory,
            allow_current_process_data_mutators=allow_current_process_data_mutators,
            _uninspectable_handle_rescans_remaining=(
                _uninspectable_handle_rescans_remaining - 1
            ),
        )


_CHILD_DACL_PROBE = r'''
import ctypes
import os
import pathlib
import shutil
import subprocess
import sys
from ctypes import wintypes

WRITE_DAC = 0x00040000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

file_path = pathlib.Path(sys.argv[1])
parent_path = pathlib.Path(sys.argv[2])
icacls = pathlib.Path(sys.argv[3])

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
close_handle = kernel32.CloseHandle
close_handle.argtypes = (wintypes.HANDLE,)
close_handle.restype = wintypes.BOOL


def fresh_write_dac_available(path: pathlib.Path, directory: bool) -> bool:
    flags = FILE_FLAG_OPEN_REPARSE_POINT | (
        FILE_FLAG_BACKUP_SEMANTICS if directory else FILE_ATTRIBUTE_NORMAL
    )
    ctypes.set_last_error(0)
    raw = create_file(
        str(path),
        WRITE_DAC,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    value = raw if isinstance(raw, int) else ctypes.cast(raw, ctypes.c_void_p).value
    if value in {None, INVALID_HANDLE_VALUE}:
        return False
    close_handle(raw)
    return True


for path in (file_path, parent_path):
    completed = subprocess.run(
        [str(icacls), str(path), "/remove:d", "*S-1-3-4", "/Q"],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if completed.returncode == 0:
        raise SystemExit(f"hostile same-token DACL rewrite unexpectedly succeeded for {path}")

if fresh_write_dac_available(file_path, False):
    raise SystemExit("hostile child acquired fresh WRITE_DAC on expected snapshot")
if fresh_write_dac_available(parent_path, True):
    raise SystemExit("hostile child acquired fresh WRITE_DAC on expected-snapshot parent")

try:
    with file_path.open("r+b") as writer:
        writer.seek(0, os.SEEK_END)
        writer.write(b"AUTOSPORT_HOSTILE_DACL_REWRITE_WRITE")
        writer.flush()
        os.fsync(writer.fileno())
except OSError:
    pass
else:
    raise SystemExit("hostile child wrote expected snapshot after DACL rewrite attempt")

replacement = file_path.with_name(f".{file_path.name}.hostile-security-replacement-{os.getpid()}")
try:
    with file_path.open("rb") as reader, replacement.open("wb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.write(b"AUTOSPORT_HOSTILE_DACL_REWRITE_REPLACEMENT")
        writer.flush()
        os.fsync(writer.fileno())
    try:
        os.replace(replacement, file_path)
    except OSError:
        pass
    else:
        raise SystemExit("hostile child replaced expected snapshot after DACL rewrite attempt")
finally:
    try:
        replacement.unlink()
    except FileNotFoundError:
        pass
'''


def install(wrapper: ModuleType) -> None:
    """Harden the existing guarded-PyInstaller wrapper without forking its release architecture."""

    if getattr(wrapper, "_EXPECTED_SNAPSHOT_SECURITY_AUTHORITY_HARDENED", False):
        return

    core = wrapper._CORE
    base_namespace_fence = wrapper._install_expected_snapshot_namespace_fence
    base_restore_dacl = wrapper._restore_windows_dacl
    base_set_file_fence = core._set_expected_snapshot_write_fence
    base_remove_file_fence = core._remove_expected_snapshot_write_fence
    base_require_access_denied = core._require_windows_access_denied

    process_authority_path = pathlib.Path(__file__).with_name(
        "guarded_pyinstaller_process_authority.py"
    )
    process_authority_spec = importlib.util.spec_from_file_location(
        "_autosport_guarded_pyinstaller_process_authority",
        process_authority_path,
    )
    if process_authority_spec is None or process_authority_spec.loader is None:
        raise RuntimeError(
            f"could not load guarded PyInstaller process authority: {process_authority_path}"
        )
    process_authority = importlib.util.module_from_spec(process_authority_spec)
    sys.modules[process_authority_spec.name] = process_authority
    process_authority_spec.loader.exec_module(process_authority)
    process_fence = process_authority.ProcessDuplicationFence(_query_system_handles)

    parent_authorities: dict[str, dict[str, Any]] = {}
    file_authorities: dict[str, dict[str, Any]] = {}

    def normalized(path: pathlib.Path) -> str:
        return core._normalized_path(path)

    def release_process_fence_if_idle() -> None:
        if (
            process_fence.installed
            and not parent_authorities
            and not file_authorities
        ):
            process_fence.release()

    def open_security_authority(
        path: pathlib.Path,
        *,
        directory: bool,
        pin_delete: bool,
    ) -> Any:
        kernel32 = core._windows_kernel32()
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
        share = core._FILE_SHARE_READ | core._FILE_SHARE_WRITE
        if not pin_delete:
            share |= core._FILE_SHARE_DELETE
        flags = core._FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= wrapper._FILE_FLAG_BACKUP_SEMANTICS
        else:
            flags |= core._FILE_ATTRIBUTE_NORMAL
        ctypes.set_last_error(0)
        raw_handle = create_file(
            str(path),
            _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
            share,
            None,
            core._OPEN_EXISTING,
            flags,
            None,
        )
        handle_value = (
            raw_handle
            if isinstance(raw_handle, int)
            else ctypes.cast(raw_handle, ctypes.c_void_p).value
        )
        if handle_value in {None, core._INVALID_HANDLE_VALUE}:
            raise ctypes.WinError(ctypes.get_last_error())
        return raw_handle

    def restore_dacl_through_handle(raw_handle: Any, descriptor: bytes) -> None:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        )
        get_dacl.restype = wintypes.BOOL
        set_security_info = advapi32.SetSecurityInfo
        set_security_info.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        set_security_info.restype = wintypes.DWORD

        buffer = ctypes.create_string_buffer(descriptor, len(descriptor))
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not get_dacl(
            ctypes.cast(buffer, ctypes.c_void_p),
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        dacl_pointer = dacl if present.value else None
        error = int(
            set_security_info(
                raw_handle,
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl_pointer,
                None,
            )
        )
        if error != 0:
            raise ctypes.WinError(error)

    def fresh_write_dac_available(path: pathlib.Path, *, directory: bool) -> bool:
        kernel32 = core._windows_kernel32()
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
        flags = core._FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= wrapper._FILE_FLAG_BACKUP_SEMANTICS
        else:
            flags |= core._FILE_ATTRIBUTE_NORMAL
        ctypes.set_last_error(0)
        raw_handle = create_file(
            str(path),
            _WRITE_DAC,
            core._FILE_SHARE_READ | core._FILE_SHARE_WRITE | core._FILE_SHARE_DELETE,
            None,
            core._OPEN_EXISTING,
            flags,
            None,
        )
        handle_value = (
            raw_handle
            if isinstance(raw_handle, int)
            else ctypes.cast(raw_handle, ctypes.c_void_p).value
        )
        if handle_value in {None, core._INVALID_HANDLE_VALUE}:
            error = ctypes.get_last_error()
            if error == core._ERROR_ACCESS_DENIED:
                return False
            raise ctypes.WinError(error)
        core._close_windows_handle(raw_handle)
        return True

    def install_owner_write_dac_lock(path: pathlib.Path) -> None:
        icacls = core._windows_system_binary("icacls.exe")
        completed = subprocess.run(
            [
                str(icacls),
                str(path),
                "/deny",
                f"*{_OWNER_RIGHTS_SID}:(WDAC)",
                "/Q",
            ],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "trusted expected-snapshot OWNER RIGHTS WRITE_DAC lock could not be installed: "
                f"icacls exited {completed.returncode}"
            )

    def restore_parent_dacl(path: pathlib.Path, descriptor: bytes) -> None:
        key = normalized(path)
        authority = parent_authorities.pop(key, None)
        if authority is None:
            base_restore_dacl(path, descriptor)
            return
        if authority["descriptor"] != descriptor:
            core._close_windows_handle(authority["handle"])
            release_process_fence_if_idle()
            raise RuntimeError("expected-snapshot parent DACL restore descriptor changed")
        try:
            restore_dacl_through_handle(authority["handle"], descriptor)
        finally:
            try:
                core._close_windows_handle(authority["handle"])
            finally:
                release_process_fence_if_idle()

    def hardened_namespace_fence(path: pathlib.Path, sid: str) -> dict[str, Any]:
        parent = pathlib.Path(path).parent
        wrapper._require_regular_directory_nonreparse(
            parent,
            label="trusted expected-snapshot parent namespace",
        )
        key = normalized(parent)
        if key in parent_authorities:
            raise RuntimeError("expected-snapshot parent security authority was installed twice")
        process_fence.acquire(sid)
        try:
            raw_handle = open_security_authority(parent, directory=True, pin_delete=True)
        except BaseException:
            release_process_fence_if_idle()
            raise
        try:
            descriptor = wrapper._capture_windows_dacl(parent)
        except BaseException:
            core._close_windows_handle(raw_handle)
            release_process_fence_if_idle()
            raise
        parent_authorities[key] = {
            "handle": raw_handle,
            "descriptor": descriptor,
        }
        try:
            record = base_namespace_fence(path, sid)
            if record["parent_dacl"] != descriptor:
                raise RuntimeError("expected-snapshot parent DACL changed while binding restore authority")
            install_owner_write_dac_lock(parent)
            if fresh_write_dac_available(parent, directory=True):
                raise RuntimeError(
                    "trusted expected-snapshot parent security fence still allows fresh WRITE_DAC"
                )
            _require_no_competing_mutation_handles(
                raw_handle,
                label="trusted expected-snapshot parent security fence",
                directory=True,
            )
            record["parent_dacl"] = descriptor
            record["parent_security_authority"] = True
            return record
        except BaseException:
            authority = parent_authorities.get(key)
            if authority is not None:
                try:
                    restore_parent_dacl(parent, descriptor)
                except BaseException:
                    pass
            raise

    def hardened_set_file_fence(path: pathlib.Path, sid: str) -> None:
        path = pathlib.Path(path)
        key = normalized(path)
        if key in file_authorities:
            raise RuntimeError("expected-snapshot file security authority was installed twice")
        process_fence.acquire(sid)
        try:
            raw_handle = open_security_authority(path, directory=False, pin_delete=False)
        except BaseException:
            release_process_fence_if_idle()
            raise
        try:
            descriptor = wrapper._capture_windows_dacl(path)
        except BaseException:
            core._close_windows_handle(raw_handle)
            release_process_fence_if_idle()
            raise
        file_authorities[key] = {
            "handle": raw_handle,
            "descriptor": descriptor,
            "path": path,
            "probe_exercised": False,
        }
        try:
            base_set_file_fence(path, sid)
            install_owner_write_dac_lock(path)
            if fresh_write_dac_available(path, directory=False):
                raise RuntimeError(
                    "trusted expected-snapshot file security fence still allows fresh WRITE_DAC"
                )
            _require_no_competing_mutation_handles(
                raw_handle,
                label="trusted expected-snapshot file security fence",
                directory=False,
                allow_current_process_data_mutators=True,
            )
        except BaseException:
            authority = file_authorities.pop(key, None)
            if authority is not None:
                try:
                    restore_dacl_through_handle(authority["handle"], descriptor)
                finally:
                    try:
                        core._close_windows_handle(authority["handle"])
                    finally:
                        release_process_fence_if_idle()
            raise

    def hardened_remove_file_fence(path: pathlib.Path, sid: str) -> None:
        path = pathlib.Path(path)
        key = normalized(path)
        authority = file_authorities.pop(key, None)
        if authority is None:
            base_remove_file_fence(path, sid)
            return
        try:
            restore_dacl_through_handle(authority["handle"], authority["descriptor"])
        finally:
            try:
                core._close_windows_handle(authority["handle"])
            finally:
                release_process_fence_if_idle()

    def run_hostile_child_probe(path: pathlib.Path, authority: dict[str, Any]) -> None:
        if authority["probe_exercised"]:
            return
        authority["probe_exercised"] = True
        parent = path.parent
        icacls = core._windows_system_binary("icacls.exe")
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                _CHILD_DACL_PROBE,
                str(path),
                str(parent),
                str(icacls),
            ],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if completed.returncode != 0:
            detail = (completed.stdout + "\n" + completed.stderr).strip()
            raise RuntimeError(
                "same-token expected-snapshot security adversary did not fail closed: "
                f"{detail or f'child exited {completed.returncode}'}"
            )
        raise RuntimeError(
            "PyInstaller post-EndUpdateResource security authority blocked hostile same-token DACL rewrite before oracle"
        )

    def hardened_require_access_denied(
        path: pathlib.Path,
        desired_access: int,
        *,
        label: str,
    ) -> None:
        if (
            os.environ.get(_TEST_DACL_REWRITE_ENV) == "1"
            and "post-EndUpdateResource ACL write exclusion" in label
        ):
            key = normalized(pathlib.Path(path))
            authority = file_authorities.get(key)
            if authority is None:
                raise RuntimeError(
                    "same-token DACL adversarial probe reached post-End without retained security authority"
                )
            run_hostile_child_probe(pathlib.Path(path), authority)
        base_require_access_denied(path, desired_access, label=label)

    wrapper._install_expected_snapshot_namespace_fence = hardened_namespace_fence
    wrapper._restore_windows_dacl = restore_parent_dacl
    core._set_expected_snapshot_write_fence = hardened_set_file_fence
    core._remove_expected_snapshot_write_fence = hardened_remove_file_fence
    core._require_windows_access_denied = hardened_require_access_denied
    wrapper._EXPECTED_SNAPSHOT_SECURITY_AUTHORITY_HARDENED = True