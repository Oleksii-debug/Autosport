from __future__ import annotations

import ctypes
import os
import pathlib
import runpy
import subprocess
import sys
import types
import uuid
from ctypes import wintypes
from typing import Any


_DANGEROUS_PROCESS_ACCESS = 0x000C006A
_PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
_SYNCHRONIZE = 0x00100000
_SAFE_PARENT_ACCESS = _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE
_DANGEROUS_THREAD_ACCESS = 0x000C17B3
_THREAD_QUERY_LIMITED_INFORMATION = 0x00000800
_SAFE_THREAD_ACCESS = _THREAD_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE
_THREAD_SET_CONTEXT = 0x00000010
_THREAD_SUSPEND_RESUME = 0x00000002
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_ERROR_ACCESS_DENIED = 5
_WAIT_OBJECT_0 = 0x00000000
_INFINITE = 0xFFFFFFFF
_SDDL_REVISION_1 = 1
_TOKEN_DUPLICATE = 0x0002
_TOKEN_QUERY = 0x0008
_DISABLE_MAX_PRIVILEGE = 0x00000001
_SE_GROUP_ENABLED = 0x00000004
_EVERYONE_SID = "S-1-1-0"
_PROTECTED_MARKER_MODULE = "_autosport_birth_protected_worker"
_PROTECTED_WORKER_ARG = "--autosport-birth-protected-worker"
_BARRIER_ENV = "AUTOSPORT_BINDER_LAUNCH_BARRIER"
_NONCE_ENV = "AUTOSPORT_BINDER_LAUNCH_NONCE"
_TEST_RETAIN_CREATOR_ENV = "AUTOSPORT_TEST_RETAIN_CREATOR_PROCESS_HANDLE"
_TEST_THREAD_SIBLING_PROBE_ENV = "AUTOSPORT_TEST_PRIMARY_THREAD_SIBLING_PROBE"


class _SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    )


class _StartupInfoW(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    )


class _ProcessInformation(ctypes.Structure):
    _fields_ = (
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    )


def _raw_handle_value(raw_handle: Any) -> int:
    value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    return int(value or 0)


def _close_handle(raw_handle: Any) -> None:
    if not _raw_handle_value(raw_handle):
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(raw_handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _current_user_sid() -> str:
    token_user = 1
    error_insufficient_buffer = 122

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD))

    class _TokenUser(ctypes.Structure):
        _fields_ = (("User", _SidAndAttributes),)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_token_information.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    convert_sid.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not open_process_token(get_current_process(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        if get_token_information(token, token_user, None, 0, ctypes.byref(required)):
            raise RuntimeError("TokenUser size probe unexpectedly succeeded without a buffer")
        if ctypes.get_last_error() != error_insufficient_buffer or required.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(
            token,
            token_user,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not convert_sid(user.User.Sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = sid_text.value
            if not value or not value.startswith("S-"):
                raise RuntimeError("current user SID conversion returned a non-canonical value")
            return value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        _close_handle(token)


def _current_process_token_is_restricted() -> bool:
    """Return whether the live process primary token is an OS restricted token."""

    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    is_token_restricted = advapi32.IsTokenRestricted
    is_token_restricted.argtypes = (wintypes.HANDLE,)
    is_token_restricted.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not open_process_token(get_current_process(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return bool(is_token_restricted(token))
    finally:
        _close_handle(token)


def _create_restricted_primary_token() -> Any:
    """Create a privilege-stripped primary token with explicit restricting SIDs."""

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    create_restricted_token = advapi32.CreateRestrictedToken
    create_restricted_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.HANDLE),
    )
    create_restricted_token.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertStringSidToSidW
    convert_sid.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    convert_sid.restype = wintypes.BOOL
    is_token_restricted = advapi32.IsTokenRestricted
    is_token_restricted.argtypes = (wintypes.HANDLE,)
    is_token_restricted.restype = wintypes.BOOL

    # IsTokenRestricted only reports a token as restricted when it contains a
    # restricting-SID list. Keep normal user/system read reachability by using
    # both the concrete user SID and Everyone as restricting SIDs while still
    # deleting nonessential privileges. This is a creation-time token property;
    # it cannot be retrofitted onto the already-running ordinary creator.
    sid_values = (_current_user_sid(), _EVERYONE_SID)
    sid_storage: list[ctypes.c_void_p] = []
    restricting_sids = (_SidAndAttributes * len(sid_values))()
    try:
        for index, sid_value in enumerate(sid_values):
            sid = ctypes.c_void_p()
            if not convert_sid(sid_value, ctypes.byref(sid)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid_storage.append(sid)
            restricting_sids[index].Sid = sid
            restricting_sids[index].Attributes = _SE_GROUP_ENABLED

        current = wintypes.HANDLE()
        if not open_process_token(
            get_current_process(),
            _TOKEN_QUERY | _TOKEN_DUPLICATE,
            ctypes.byref(current),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        restricted = wintypes.HANDLE()
        try:
            ctypes.set_last_error(0)
            if not create_restricted_token(
                current,
                _DISABLE_MAX_PRIVILEGE,
                0,
                None,
                0,
                None,
                len(sid_values),
                ctypes.cast(restricting_sids, ctypes.c_void_p),
                ctypes.byref(restricted),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _close_handle(current)
    finally:
        for sid in sid_storage:
            kernel32.LocalFree(sid)

    if not _raw_handle_value(restricted):
        raise RuntimeError("CreateRestrictedToken returned an empty protected-worker token")
    if not is_token_restricted(restricted):
        _close_handle(restricted)
        raise RuntimeError("protected-worker primary token lacks restricting SIDs")
    return restricted


def _birth_security_descriptor(current_user_sid: str) -> ctypes.c_void_p:
    """Create a self-relative process SD that denies dangerous rights from birth."""

    if not current_user_sid.startswith("S-"):
        raise RuntimeError("protected worker launch requires a canonical current-user SID")
    sddl = (
        "D:P"
        f"(D;;0x{_DANGEROUS_PROCESS_ACCESS:08x};;;{current_user_sid})"
        f"(D;;0x{_DANGEROUS_PROCESS_ACCESS:08x};;;OW)"
        f"(A;;0x{_SAFE_PARENT_ACCESS:08x};;;{current_user_sid})"
        "(A;;GA;;;SY)"
        "(A;;GA;;;BA)"
    )
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    length = wintypes.DWORD(0)
    if not convert(sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not descriptor.value or length.value == 0:
        if descriptor.value:
            ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(descriptor)
        raise RuntimeError("protected worker launch security descriptor is empty")
    return descriptor


def _birth_thread_security_descriptor(current_user_sid: str) -> ctypes.c_void_p:
    """Create a primary-thread SD that denies same-token mutation/control at birth."""

    if not current_user_sid.startswith("S-"):
        raise RuntimeError("protected worker launch requires a canonical current-user SID")
    sddl = (
        "D:P"
        f"(D;;0x{_DANGEROUS_THREAD_ACCESS:08x};;;{current_user_sid})"
        f"(D;;0x{_DANGEROUS_THREAD_ACCESS:08x};;;OW)"
        f"(A;;0x{_SAFE_THREAD_ACCESS:08x};;;{current_user_sid})"
        "(A;;GA;;;SY)"
        "(A;;GA;;;BA)"
    )
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    length = wintypes.DWORD(0)
    if not convert(sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not descriptor.value or length.value == 0:
        if descriptor.value:
            ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(descriptor)
        raise RuntimeError("protected worker primary-thread security descriptor is empty")
    return descriptor


_THREAD_SIBLING_PROBE = r'''
import ctypes
import sys
from ctypes import wintypes

ERROR_ACCESS_DENIED = 5
rights = (
    (0x00000010, "THREAD_SET_CONTEXT"),
    (0x00000002, "THREAD_SUSPEND_RESUME"),
    (0x00040000, "WRITE_DAC"),
    (0x00080000, "WRITE_OWNER"),
)
thread_id = int(sys.argv[1])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
open_thread = kernel32.OpenThread
open_thread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
open_thread.restype = wintypes.HANDLE
close_handle = kernel32.CloseHandle
close_handle.argtypes = (wintypes.HANDLE,)
close_handle.restype = wintypes.BOOL
for access, name in rights:
    ctypes.set_last_error(0)
    handle = open_thread(access, False, thread_id)
    value = handle if isinstance(handle, int) else ctypes.cast(handle, ctypes.c_void_p).value
    if value:
        close_handle(handle)
        raise SystemExit(f"hostile sibling acquired {name} on barrier-blocked primary thread")
    error = ctypes.get_last_error()
    if error != ERROR_ACCESS_DENIED:
        raise SystemExit(f"hostile sibling {name} probe failed non-deny: {error}")
'''


def protected_launch_attested() -> bool:
    """Accept launch attestation only when marker and OS restricted token both agree."""

    marker = sys.modules.get(_PROTECTED_MARKER_MODULE)
    nonce = getattr(marker, "nonce", None)
    restricted = getattr(marker, "restricted_primary_token", False)
    launcher_path = getattr(marker, "launcher_path", None)
    if not isinstance(nonce, str) or len(nonce) < 32 or restricted is not True:
        return False
    if not isinstance(launcher_path, str):
        return False
    try:
        if pathlib.Path(launcher_path).resolve() != pathlib.Path(__file__).resolve():
            return False
    except OSError:
        return False
    return _current_process_token_is_restricted()


def _require_fresh_thread_access_denied(thread_id: int) -> None:
    """Prove the birth DACL blocks fresh same-token primary-thread control handles."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_thread = kernel32.OpenThread
    open_thread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_thread.restype = wintypes.HANDLE
    for access, name in (
        (_THREAD_SET_CONTEXT, "THREAD_SET_CONTEXT"),
        (_THREAD_SUSPEND_RESUME, "THREAD_SUSPEND_RESUME"),
        (_WRITE_DAC, "WRITE_DAC"),
        (_WRITE_OWNER, "WRITE_OWNER"),
    ):
        ctypes.set_last_error(0)
        raw_handle = open_thread(access, False, thread_id)
        if _raw_handle_value(raw_handle):
            _close_handle(raw_handle)
            raise RuntimeError(
                f"protected worker primary-thread birth fence still allows fresh {name}"
            )
        error = ctypes.get_last_error()
        if error != _ERROR_ACCESS_DENIED:
            raise ctypes.WinError(error)


def _run_primary_thread_sibling_probe(python: pathlib.Path, thread_id: int) -> None:
    completed = subprocess.run(
        [str(python), "-I", "-c", _THREAD_SIBLING_PROBE, str(thread_id)],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "protected worker primary-thread hostile sibling probe failed: "
            + (completed.stdout + "\n" + completed.stderr).strip()
        )


def _run_birth_protected_worker() -> None:
    """Execute the immutable sibling binder after the creator releases the barrier."""

    if os.name != "nt":
        raise RuntimeError("birth-protected binder worker requires Windows")
    if not _current_process_token_is_restricted():
        raise RuntimeError("birth-protected binder worker lacks restricted primary token")

    barrier_name = os.environ.pop(_BARRIER_ENV, "")
    nonce = os.environ.pop(_NONCE_ENV, "")
    if not barrier_name or not nonce or len(nonce) < 32:
        raise RuntimeError("protected binder launch attestation is missing")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_event = kernel32.OpenEventW
    open_event.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    open_event.restype = wintypes.HANDLE
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD

    handle = open_event(_SYNCHRONIZE, False, barrier_name)
    if not _raw_handle_value(handle):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = int(wait(handle, 60_000))
    finally:
        _close_handle(handle)
    if result != _WAIT_OBJECT_0:
        raise RuntimeError(f"protected binder launch barrier wait failed: 0x{result:08x}")

    launcher = pathlib.Path(__file__).resolve()
    binder = launcher.with_name("guarded_pyinstaller_bind.py")
    if not binder.is_file():
        raise RuntimeError("protected binder launch cannot resolve immutable sibling binder")

    marker = types.ModuleType(_PROTECTED_MARKER_MODULE)
    marker.nonce = nonce
    marker.restricted_primary_token = True
    marker.launcher_path = str(launcher)
    marker.binder_path = str(binder)
    sys.modules[_PROTECTED_MARKER_MODULE] = marker

    binder_args = list(sys.argv[2:])
    sys.argv = [str(binder), *binder_args]
    runpy.run_path(str(binder), run_name="__main__")


def relaunch_birth_protected_worker() -> int:
    """Run the guarded binder behind birth DACLs and a restricted primary token.

    The ordinary creator is never accepted as trusted release execution. It may only
    create a new worker whose process/thread DACLs and restricted primary token exist
    at process birth. The child's first Python file is this immutable exact-source
    launch boundary, not a creator-memory ``python -c`` payload. The creator closes
    full process/thread handles before releasing the barrier and retains only a
    query/synchronize process handle while waiting for the protected worker.
    """

    if os.name != "nt":
        raise RuntimeError("birth-protected binder launch requires Windows")
    binder = pathlib.Path(sys.argv[0]).resolve()
    launcher = pathlib.Path(__file__).resolve()
    expected_binder = launcher.with_name("guarded_pyinstaller_bind.py")
    if binder != expected_binder:
        raise RuntimeError(
            "birth-protected launch requires the immutable sibling guarded PyInstaller binder"
        )
    python = pathlib.Path(sys.executable).resolve()
    if not python.is_file():
        raise RuntimeError("birth-protected launch cannot resolve the Python executable")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    create_event = kernel32.CreateEventW
    create_event.argtypes = (
        ctypes.POINTER(_SecurityAttributes),
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    create_event.restype = wintypes.HANDLE
    set_event = kernel32.SetEvent
    set_event.argtypes = (wintypes.HANDLE,)
    set_event.restype = wintypes.BOOL
    # Do not fall back to CreateProcessW: a restricted primary token is part of
    # the birth attestation and cannot be retrofitted into the ordinary creator.
    create_process = advapi32.CreateProcessAsUserW
    create_process.argtypes = (
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(_SecurityAttributes),
        ctypes.POINTER(_SecurityAttributes),
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfoW),
        ctypes.POINTER(_ProcessInformation),
    )
    create_process.restype = wintypes.BOOL
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL

    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    barrier_name = "Local\\AutosportBinderLaunch-" + uuid.uuid4().hex
    barrier = create_event(None, True, False, barrier_name)
    if not _raw_handle_value(barrier):
        raise ctypes.WinError(ctypes.get_last_error())

    current_user_sid = _current_user_sid()
    descriptor = _birth_security_descriptor(current_user_sid)
    thread_descriptor = _birth_thread_security_descriptor(current_user_sid)
    restricted_token = _create_restricted_primary_token()
    process_attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes),
        descriptor,
        False,
    )
    thread_attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes),
        thread_descriptor,
        False,
    )
    startup = _StartupInfoW()
    startup.cb = ctypes.sizeof(_StartupInfoW)
    process_info = _ProcessInformation()
    safe_process = wintypes.HANDLE()
    old_barrier = os.environ.get(_BARRIER_ENV)
    old_nonce = os.environ.get(_NONCE_ENV)
    os.environ[_BARRIER_ENV] = barrier_name
    os.environ[_NONCE_ENV] = nonce
    command = subprocess.list2cmdline(
        [
            str(python),
            "-I",
            str(launcher),
            _PROTECTED_WORKER_ARG,
            *sys.argv[1:],
        ]
    )
    command_buffer = ctypes.create_unicode_buffer(command)
    retain_creator = os.environ.get(_TEST_RETAIN_CREATOR_ENV) == "1"
    creator_process_open = False
    primary_thread_open = False
    try:
        ctypes.set_last_error(0)
        if not create_process(
            restricted_token,
            str(python),
            command_buffer,
            ctypes.byref(process_attributes),
            ctypes.byref(thread_attributes),
            False,
            0,
            None,
            os.getcwd(),
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        creator_process_open = True
        primary_thread_open = True

        _require_fresh_thread_access_denied(int(process_info.dwThreadId))
        if os.environ.get(_TEST_THREAD_SIBLING_PROBE_ENV) == "1":
            _run_primary_thread_sibling_probe(python, int(process_info.dwThreadId))

        ctypes.set_last_error(0)
        safe_process = open_process(_SAFE_PARENT_ACCESS, False, process_info.dwProcessId)
        if not _raw_handle_value(safe_process):
            raise ctypes.WinError(ctypes.get_last_error())

        _close_handle(process_info.hThread)
        primary_thread_open = False
        if not retain_creator:
            _close_handle(process_info.hProcess)
            creator_process_open = False

        if not set_event(barrier):
            raise ctypes.WinError(ctypes.get_last_error())
        if int(wait(safe_process, _INFINITE)) != _WAIT_OBJECT_0:
            raise RuntimeError("protected worker wait did not reach signalled process state")
        exit_code = wintypes.DWORD(0)
        if not get_exit_code(safe_process, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(exit_code.value)
    finally:
        if creator_process_open:
            _close_handle(process_info.hProcess)
        if primary_thread_open:
            _close_handle(process_info.hThread)
        if _raw_handle_value(safe_process):
            _close_handle(safe_process)
        _close_handle(restricted_token)
        _close_handle(barrier)
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(thread_descriptor)
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(descriptor)
        if old_barrier is None:
            os.environ.pop(_BARRIER_ENV, None)
        else:
            os.environ[_BARRIER_ENV] = old_barrier
        if old_nonce is None:
            os.environ.pop(_NONCE_ENV, None)
        else:
            os.environ[_NONCE_ENV] = old_nonce


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != _PROTECTED_WORKER_ARG:
        raise SystemExit("guarded PyInstaller launch boundary is internal-only")
    _run_birth_protected_worker()
