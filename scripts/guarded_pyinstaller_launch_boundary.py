from __future__ import annotations

import ctypes
import os
import pathlib
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
_PROTECTED_MARKER_MODULE = "_autosport_birth_protected_worker"
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
    token_query = 0x0008
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
    if not open_process_token(get_current_process(), token_query, ctypes.byref(token)):
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


def _birth_security_descriptor(current_user_sid: str) -> ctypes.c_void_p:
    """Create a self-relative process SD that denies dangerous rights from birth."""

    if not current_user_sid.startswith("S-"):
        raise RuntimeError("protected worker launch requires a canonical current-user SID")
    # Deny process injection/duplication/security rewrite to both the concrete user
    # and OWNER RIGHTS while retaining only query/synchronize for the launching user.
    # SYSTEM/Administrators keep their normal administrative access; the explicit
    # user deny takes precedence when the current token also belongs to Administrators.
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


_PROTECTED_BOOTSTRAP = r'''
import ctypes
import os
import runpy
import sys
import types
from ctypes import wintypes

barrier_name = os.environ.pop("AUTOSPORT_BINDER_LAUNCH_BARRIER", "")
nonce = os.environ.pop("AUTOSPORT_BINDER_LAUNCH_NONCE", "")
if not barrier_name or not nonce:
    raise SystemExit("protected binder launch attestation is missing")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
open_event = kernel32.OpenEventW
open_event.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
open_event.restype = wintypes.HANDLE
wait = kernel32.WaitForSingleObject
wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
wait.restype = wintypes.DWORD
close_handle = kernel32.CloseHandle
close_handle.argtypes = (wintypes.HANDLE,)
close_handle.restype = wintypes.BOOL
handle = open_event(0x00100000, False, barrier_name)
value = handle if isinstance(handle, int) else ctypes.cast(handle, ctypes.c_void_p).value
if not value:
    raise SystemExit(f"protected binder launch barrier open failed: {ctypes.get_last_error()}")
try:
    result = int(wait(handle, 60000))
finally:
    close_handle(handle)
if result != 0:
    raise SystemExit(f"protected binder launch barrier wait failed: 0x{result:08x}")
marker = types.ModuleType("_autosport_birth_protected_worker")
marker.nonce = nonce
sys.modules["_autosport_birth_protected_worker"] = marker
script = sys.argv[1]
sys.argv = [script, *sys.argv[2:]]
runpy.run_path(script, run_name="__main__")
'''


def protected_launch_attested() -> bool:
    marker = sys.modules.get(_PROTECTED_MARKER_MODULE)
    nonce = getattr(marker, "nonce", None)
    return isinstance(nonce, str) and len(nonce) >= 32


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


def relaunch_birth_protected_worker() -> int:
    """Run the guarded binder in a process protected before its first instruction.

    The child starts in a tiny stdlib-only barrier bootstrap under a birth DACL that
    denies dangerous same-token process rights. The creator closes its full process
    and primary-thread handles before signalling the barrier. It retains only a
    query/synchronize process handle, which the worker's live handle census permits.
    """

    if os.name != "nt":
        raise RuntimeError("birth-protected binder launch requires Windows")
    binder = pathlib.Path(sys.argv[0]).resolve()
    if binder.name.lower() != "guarded_pyinstaller_bind.py":
        raise RuntimeError("birth-protected launch was requested outside guarded PyInstaller binder")
    python = pathlib.Path(sys.executable).resolve()
    if not python.is_file():
        raise RuntimeError("birth-protected launch cannot resolve the Python executable")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    create_process = kernel32.CreateProcessW
    create_process.argtypes = (
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
            "-c",
            _PROTECTED_BOOTSTRAP,
            str(binder),
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

        # The primary thread handle can steer execution; dispose it while the child
        # is still blocked in the launch bootstrap. Normal production also closes the
        # creator's full hProcess before releasing that barrier. The test hook retains
        # hProcess deliberately so the worker's real system-handle audit must reject.
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
