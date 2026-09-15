from __future__ import annotations

import ctypes
import importlib.util
import os
import pathlib
import subprocess
import sys
from ctypes import wintypes
from typing import Any, Callable


_OWNER_RIGHTS_SID = "S-1-3-4"
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_PROCESS_CREATE_THREAD = 0x00000002
_PROCESS_VM_OPERATION = 0x00000008
_PROCESS_VM_WRITE = 0x00000020
_PROCESS_DUP_HANDLE = 0x00000040
_PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_KERNEL_OBJECT = 6
_ERROR_ACCESS_DENIED = 5
_DENY_ACCESS = 3
_NO_INHERITANCE = 0
_NO_MULTIPLE_TRUSTEE = 0
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_UNKNOWN = 0
_SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_MAX_SYSTEM_HANDLE_SNAPSHOT_BYTES = 64 * 1024 * 1024
_TEST_PRELAUNCH_DUP_HANDLE_ENV = "AUTOSPORT_TEST_PRELAUNCH_PROCESS_DUP_HANDLE_HELPER"
_DANGEROUS_PROCESS_ACCESS = (
    _PROCESS_CREATE_THREAD
    | _PROCESS_VM_OPERATION
    | _PROCESS_VM_WRITE
    | _PROCESS_DUP_HANDLE
    | _WRITE_DAC
    | _WRITE_OWNER
)


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


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = (
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_size_t),
        ("InheritedFromUniqueProcessId", ctypes.c_size_t),
    )


_TEST_DUP_HANDLE_HELPER = r'''
import ctypes
import sys
from ctypes import wintypes

PROCESS_DUP_HANDLE = 0x00000040
pid = int(sys.argv[1])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
open_process = kernel32.OpenProcess
open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
open_process.restype = wintypes.HANDLE
close_handle = kernel32.CloseHandle
close_handle.argtypes = (wintypes.HANDLE,)
close_handle.restype = wintypes.BOOL
ctypes.set_last_error(0)
handle = open_process(PROCESS_DUP_HANDLE, False, pid)
value = handle if isinstance(handle, int) else ctypes.cast(handle, ctypes.c_void_p).value
if not value:
    raise SystemExit(f"OpenProcess(PROCESS_DUP_HANDLE) failed: {ctypes.get_last_error()}")
print("READY", flush=True)
try:
    sys.stdin.buffer.read(1)
finally:
    close_handle(handle)
'''


def _query_system_handles() -> list[tuple[int, int, int, int]]:
    """Capture process-handle authority from the Windows extended handle table."""

    if os.name != "nt":
        raise RuntimeError("process handle authority audit requires Windows")
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
                raise RuntimeError("process handle authority snapshot header was truncated")
            count = ctypes.c_size_t.from_buffer_copy(
                buffer.raw[: ctypes.sizeof(ctypes.c_size_t)]
            ).value
            entry_size = ctypes.sizeof(_SystemHandleTableEntryInfoEx)
            required = header_size + int(count) * entry_size
            if required > size:
                raise RuntimeError("process handle authority snapshot entries were truncated")
            snapshot: list[tuple[int, int, int, int]] = []
            for index in range(int(count)):
                offset = header_size + index * entry_size
                entry = _SystemHandleTableEntryInfoEx.from_buffer_copy(
                    buffer.raw[offset : offset + entry_size]
                )
                snapshot.append(
                    (
                        int(entry.Object or 0),
                        int(entry.UniqueProcessId),
                        int(entry.HandleValue),
                        int(entry.GrantedAccess),
                    )
                )
            return snapshot
        if status_u32 != _STATUS_INFO_LENGTH_MISMATCH:
            raise RuntimeError(
                "process handle authority snapshot failed: "
                f"NTSTATUS=0x{status_u32:08x}"
            )
        requested = int(returned.value)
        size = max(size * 2, requested + 64 * 1024)
    raise RuntimeError("process handle authority snapshot exceeded bounded capture size")


def _parent_process_id() -> int:
    """Return the exact immediate creator PID of the current launcher process."""

    if os.name != "nt":
        raise RuntimeError("parent process identity requires Windows")
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = ntdll.NtQueryInformationProcess
    query.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    )
    query.restype = ctypes.c_long
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE

    info = _ProcessBasicInformation()
    returned = wintypes.ULONG(0)
    status = int(
        query(
            get_current_process(),
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
    )
    if status != 0:
        raise RuntimeError(
            "parent process identity query failed: "
            f"NTSTATUS=0x{ctypes.c_uint32(status).value:08x}"
        )
    if returned.value and returned.value < ctypes.sizeof(info):
        raise RuntimeError("parent process identity query returned a truncated structure")
    parent_pid = int(info.InheritedFromUniqueProcessId)
    if parent_pid <= 0 or parent_pid == os.getpid():
        raise RuntimeError("parent process identity query returned an invalid creator PID")
    return parent_pid


def _spawn_test_prelaunch_dup_handle_helper() -> subprocess.Popen[str] | None:
    if os.environ.get(_TEST_PRELAUNCH_DUP_HANDLE_ENV) != "1":
        return None
    helper = subprocess.Popen(
        [sys.executable, "-I", "-c", _TEST_DUP_HANDLE_HELPER, str(os.getpid())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert helper.stdout is not None
    ready = helper.stdout.readline().strip()
    if ready == "READY":
        return helper
    stderr = ""
    if helper.stderr is not None:
        stderr = helper.stderr.read().strip()
    helper.kill()
    helper.wait()
    raise RuntimeError(
        "prelaunch PROCESS_DUP_HANDLE test helper could not acquire authority: "
        f"{ready or stderr or f'exit {helper.returncode}'}"
    )


def _stop_test_prelaunch_dup_handle_helper(
    helper: subprocess.Popen[str] | None,
) -> None:
    if helper is None:
        return
    if helper.stdin is not None:
        helper.stdin.close()
    try:
        helper.wait(timeout=5)
    except subprocess.TimeoutExpired:
        helper.kill()
        helper.wait()


def _require_birth_protected_worker(
    query_system_handles: Callable[[], list[tuple[int, int, int, int]]],
) -> None:
    """Move the release-sensitive binder behind a protected creator handoff."""

    if os.name != "nt":
        return
    if pathlib.Path(sys.argv[0]).name.lower() != "guarded_pyinstaller_bind.py":
        return
    launcher_path = pathlib.Path(__file__).with_name(
        "guarded_pyinstaller_launch_boundary.py"
    )
    launcher_spec = importlib.util.spec_from_file_location(
        "_autosport_guarded_pyinstaller_launch_boundary",
        launcher_path,
    )
    if launcher_spec is None or launcher_spec.loader is None:
        raise RuntimeError(
            f"could not load guarded PyInstaller launch boundary: {launcher_path}"
        )
    launcher = importlib.util.module_from_spec(launcher_spec)
    sys.modules[launcher_spec.name] = launcher
    launcher_spec.loader.exec_module(launcher)
    if launcher.protected_launch_attested():
        return

    trusted_creator_pid = _parent_process_id()
    helper = _spawn_test_prelaunch_dup_handle_helper()
    creator_fence = ProcessDuplicationFence(
        query_system_handles,
        allowed_external_pids={trusted_creator_pid},
    )
    try:
        creator_fence.acquire(launcher._current_user_sid())
        if helper is not None:
            raise RuntimeError(
                "prelaunch PROCESS_DUP_HANDLE adversary unexpectedly survived creator fence"
            )
        exit_code = launcher.relaunch_birth_protected_worker()
    finally:
        try:
            creator_fence.release()
        finally:
            _stop_test_prelaunch_dup_handle_helper(helper)
    raise SystemExit(exit_code)


class _TrusteeW(ctypes.Structure):
    _fields_ = (
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", wintypes.LPWSTR),
    )


class _ExplicitAccessW(ctypes.Structure):
    _fields_ = (
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", _TrusteeW),
    )


def _raw_handle_value(raw_handle: Any) -> int:
    value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    if value is None:
        raise RuntimeError("process authority handle has no value")
    return int(value)


def _local_free(pointer: Any) -> None:
    if not pointer:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    result = local_free(pointer)
    if result:
        raise ctypes.WinError(ctypes.get_last_error())


def _capture_kernel_object_dacl(raw_handle: Any) -> tuple[bytes, ctypes.c_void_p, ctypes.c_void_p]:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security_info = advapi32.GetSecurityInfo
    get_security_info.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    get_security_info.restype = wintypes.DWORD
    get_length = advapi32.GetSecurityDescriptorLength
    get_length.argtypes = (ctypes.c_void_p,)
    get_length.restype = wintypes.DWORD

    dacl = ctypes.c_void_p()
    security_descriptor = ctypes.c_void_p()
    error = int(
        get_security_info(
            raw_handle,
            _SE_KERNEL_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
    )
    if error != 0:
        raise ctypes.WinError(error)
    if not security_descriptor.value:
        raise RuntimeError("current process security descriptor is unavailable")
    length = int(get_length(security_descriptor))
    if length <= 0:
        _local_free(security_descriptor)
        raise RuntimeError("current process security descriptor has invalid length")
    descriptor = bytes(ctypes.string_at(security_descriptor, length))
    return descriptor, dacl, security_descriptor


def _restore_kernel_object_dacl(raw_handle: Any, descriptor: bytes) -> None:
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
    error = int(
        set_security_info(
            raw_handle,
            _SE_KERNEL_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl if present.value else None,
            None,
        )
    )
    if error != 0:
        raise ctypes.WinError(error)


def _sid_from_string(value: str) -> ctypes.c_void_p:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSidToSidW
    convert.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    convert.restype = wintypes.BOOL
    sid = ctypes.c_void_p()
    if not convert(value, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return sid


def _deny_entries_for_sids(sids: list[ctypes.c_void_p]) -> Any:
    entries = (_ExplicitAccessW * len(sids))()
    for index, sid in enumerate(sids):
        entries[index].grfAccessPermissions = _DANGEROUS_PROCESS_ACCESS
        entries[index].grfAccessMode = _DENY_ACCESS
        entries[index].grfInheritance = _NO_INHERITANCE
        entries[index].Trustee.pMultipleTrustee = None
        entries[index].Trustee.MultipleTrusteeOperation = _NO_MULTIPLE_TRUSTEE
        entries[index].Trustee.TrusteeForm = _TRUSTEE_IS_SID
        entries[index].Trustee.TrusteeType = _TRUSTEE_IS_UNKNOWN
        entries[index].Trustee.ptstrName = ctypes.cast(sid, wintypes.LPWSTR)
    return entries


def _install_process_deny(raw_handle: Any, current_user_sid: str) -> bytes:
    descriptor, old_dacl, security_descriptor = _capture_kernel_object_dacl(raw_handle)
    if not old_dacl.value:
        _local_free(security_descriptor)
        raise RuntimeError("current process DACL is NULL; refusing non-preserving authority rewrite")

    current_sid = _sid_from_string(current_user_sid)
    owner_rights_sid = _sid_from_string(_OWNER_RIGHTS_SID)
    new_acl = ctypes.c_void_p()
    try:
        entries = _deny_entries_for_sids([current_sid, owner_rights_sid])
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        set_entries = advapi32.SetEntriesInAclW
        set_entries.argtypes = (
            wintypes.ULONG,
            ctypes.POINTER(_ExplicitAccessW),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        set_entries.restype = wintypes.DWORD
        error = int(
            set_entries(
                len(entries),
                ctypes.cast(entries, ctypes.POINTER(_ExplicitAccessW)),
                old_dacl,
                ctypes.byref(new_acl),
            )
        )
        if error != 0:
            raise ctypes.WinError(error)

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
        error = int(
            set_security_info(
                raw_handle,
                _SE_KERNEL_OBJECT,
                _DACL_SECURITY_INFORMATION,
                None,
                None,
                new_acl,
                None,
            )
        )
        if error != 0:
            raise ctypes.WinError(error)
        return descriptor
    finally:
        _local_free(new_acl)
        _local_free(current_sid)
        _local_free(owner_rights_sid)
        _local_free(security_descriptor)


def _fresh_process_access_available(desired_access: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    raw_handle = open_process(desired_access, False, os.getpid())
    value = raw_handle if isinstance(raw_handle, int) else ctypes.cast(raw_handle, ctypes.c_void_p).value
    if value in {None, 0}:
        error = ctypes.get_last_error()
        if error == _ERROR_ACCESS_DENIED:
            return False
        raise ctypes.WinError(error)
    if not close_handle(raw_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    return True


def _open_self_identity_handle() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    raw_handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, os.getpid())
    value = raw_handle if isinstance(raw_handle, int) else ctypes.cast(raw_handle, ctypes.c_void_p).value
    if value in {None, 0}:
        raise ctypes.WinError(ctypes.get_last_error())
    return raw_handle


def _close_handle(raw_handle: Any) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(raw_handle):
        raise ctypes.WinError(ctypes.get_last_error())


class ProcessDuplicationFence:
    """Deny same-token process access that can duplicate or hijack retained authorities."""

    def __init__(
        self,
        query_system_handles: Callable[[], list[tuple[int, int, int, int]]],
        *,
        allowed_external_pids: set[int] | None = None,
    ) -> None:
        self._query_system_handles = query_system_handles
        self._allowed_external_pids = frozenset(allowed_external_pids or ())
        self._descriptor: bytes | None = None

    @property
    def installed(self) -> bool:
        return self._descriptor is not None

    def _require_fresh_dangerous_access_denied(self) -> None:
        for access, name in (
            (_PROCESS_CREATE_THREAD, "PROCESS_CREATE_THREAD"),
            (_PROCESS_VM_OPERATION, "PROCESS_VM_OPERATION"),
            (_PROCESS_VM_WRITE, "PROCESS_VM_WRITE"),
            (_PROCESS_DUP_HANDLE, "PROCESS_DUP_HANDLE"),
            (_WRITE_DAC, "WRITE_DAC"),
            (_WRITE_OWNER, "WRITE_OWNER"),
        ):
            if _fresh_process_access_available(access):
                raise RuntimeError(
                    f"trusted build-process security fence still allows fresh {name}"
                )

    def _require_no_untrusted_preexisting_authority(self) -> None:
        identity_handle = _open_self_identity_handle()
        try:
            identity_value = _raw_handle_value(identity_handle)
            snapshot = self._query_system_handles()
            current_pid = os.getpid()
            identity_rows = [
                row
                for row in snapshot
                if row[1] == current_pid and row[2] == identity_value
            ]
            if len(identity_rows) != 1 or identity_rows[0][0] == 0:
                raise RuntimeError(
                    "trusted build-process identity handle was not uniquely visible in system handle table"
                )
            process_object = identity_rows[0][0]
            competing = [
                row
                for row in snapshot
                if row[0] == process_object
                and row[3] & _DANGEROUS_PROCESS_ACCESS
                and row[1] != current_pid
                and row[1] not in self._allowed_external_pids
            ]
            if competing:
                raise RuntimeError(
                    "trusted build-process security fence found "
                    f"{len(competing)} pre-existing external dangerous process handle(s)"
                )
        finally:
            _close_handle(identity_handle)

    def acquire(self, current_user_sid: str) -> None:
        if self.installed:
            return
        if os.name != "nt":
            raise RuntimeError("build-process security authority requires Windows")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = ()
        get_current_process.restype = wintypes.HANDLE
        current_process = get_current_process()
        descriptor = _install_process_deny(current_process, current_user_sid)
        try:
            self._require_fresh_dangerous_access_denied()
            self._require_no_untrusted_preexisting_authority()
            # The birth-protected worker DACL prevents peers from ever obtaining a
            # fresh dangerous handle before this point. This repeated proof keeps the
            # narrower post-install DACL mutation check fail-closed as a second layer.
            self._require_fresh_dangerous_access_denied()
        except BaseException:
            _restore_kernel_object_dacl(current_process, descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = ()
        get_current_process.restype = wintypes.HANDLE
        current_process = get_current_process()
        try:
            _restore_kernel_object_dacl(current_process, descriptor)
        finally:
            self._descriptor = None


_require_birth_protected_worker(_query_system_handles)
