from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import pathlib
import subprocess
import sys
import uuid
from ctypes import wintypes
from typing import Any

_MARKER_ENV = "AUTOSPORT_RELEASE_CREATOR_BIRTH_PROTECTED"
_BARRIER_ENV = "AUTOSPORT_RELEASE_CREATOR_BARRIER"
_NONCE_ENV = "AUTOSPORT_RELEASE_CREATOR_NONCE"
_ERROR_ACCESS_DENIED = 5
_DANGEROUS_PROCESS_RIGHTS = (
    0x00000002,
    0x00000008,
    0x00000020,
    0x00000040,
    0x00040000,
    0x00080000,
)


def _load_boundary() -> Any:
    path = pathlib.Path(__file__).with_name("guarded_pyinstaller_launch_boundary.py").resolve()
    spec = importlib.util.spec_from_file_location("_autosport_release_birth_primitives", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load guarded release birth primitives")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_handle_value(raw_handle: Any) -> int:
    if isinstance(raw_handle, int):
        return raw_handle
    return int(ctypes.cast(raw_handle, ctypes.c_void_p).value or 0)


def _require_fresh_process_access_denied(pid: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    for access in _DANGEROUS_PROCESS_RIGHTS:
        ctypes.set_last_error(0)
        handle = open_process(access, False, pid)
        if _raw_handle_value(handle):
            close_handle(handle)
            raise RuntimeError(
                f"birth-protected release creator still allows fresh process access 0x{access:08x}"
            )
        error = ctypes.get_last_error()
        if error != _ERROR_ACCESS_DENIED:
            raise ctypes.WinError(error)


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("birth-protected release creator launch requires Windows")
    parser = argparse.ArgumentParser()
    parser.add_argument("--powershell", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--working-directory", required=True)
    args = parser.parse_args()

    powershell = pathlib.Path(args.powershell).resolve()
    script = pathlib.Path(args.script).resolve()
    working_directory = pathlib.Path(args.working_directory).resolve()
    if not powershell.is_file():
        raise RuntimeError("release creator launch cannot resolve PowerShell executable")
    if not script.is_file():
        raise RuntimeError("release creator launch cannot resolve build script")
    if not working_directory.is_dir():
        raise RuntimeError("release creator launch cannot resolve working directory")

    boundary = _load_boundary()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    create_event = kernel32.CreateEventW
    create_event.argtypes = (
        ctypes.POINTER(boundary._SecurityAttributes),
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    create_event.restype = wintypes.HANDLE
    set_event = kernel32.SetEvent
    set_event.argtypes = (wintypes.HANDLE,)
    set_event.restype = wintypes.BOOL
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    create_process = advapi32.CreateProcessAsUserW
    create_process.argtypes = (
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(boundary._SecurityAttributes),
        ctypes.POINTER(boundary._SecurityAttributes),
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(boundary._StartupInfoW),
        ctypes.POINTER(boundary._ProcessInformation),
    )
    create_process.restype = wintypes.BOOL

    barrier_name = r"Local\AutosportReleaseCreator-" + uuid.uuid4().hex
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    barrier = create_event(None, True, False, barrier_name)
    if not _raw_handle_value(barrier):
        raise ctypes.WinError(ctypes.get_last_error())

    current_user_sid = boundary._current_user_sid()
    process_descriptor = boundary._birth_security_descriptor(current_user_sid)
    thread_descriptor = boundary._birth_thread_security_descriptor(current_user_sid)
    restricted_token = boundary._create_restricted_primary_token()
    process_attributes = boundary._SecurityAttributes(
        ctypes.sizeof(boundary._SecurityAttributes), process_descriptor, False
    )
    thread_attributes = boundary._SecurityAttributes(
        ctypes.sizeof(boundary._SecurityAttributes), thread_descriptor, False
    )
    startup = boundary._StartupInfoW()
    startup.cb = ctypes.sizeof(boundary._StartupInfoW)
    process_info = boundary._ProcessInformation()
    safe_process = wintypes.HANDLE()
    old_values = {name: os.environ.get(name) for name in (_MARKER_ENV, _BARRIER_ENV, _NONCE_ENV)}
    os.environ[_MARKER_ENV] = "1"
    os.environ[_BARRIER_ENV] = barrier_name
    os.environ[_NONCE_ENV] = nonce
    command = subprocess.list2cmdline(
        [str(powershell), "-NoProfile", "-NonInteractive", "-File", str(script)]
    )
    command_buffer = ctypes.create_unicode_buffer(command)
    creator_process_open = False
    primary_thread_open = False
    try:
        ctypes.set_last_error(0)
        if not create_process(
            restricted_token,
            str(powershell),
            command_buffer,
            ctypes.byref(process_attributes),
            ctypes.byref(thread_attributes),
            False,
            0,
            None,
            str(working_directory),
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        creator_process_open = True
        primary_thread_open = True

        _require_fresh_process_access_denied(int(process_info.dwProcessId))
        boundary._require_fresh_thread_access_denied(int(process_info.dwThreadId))

        safe_process = open_process(boundary._SAFE_PARENT_ACCESS, False, process_info.dwProcessId)
        if not _raw_handle_value(safe_process):
            raise ctypes.WinError(ctypes.get_last_error())

        boundary._close_handle(process_info.hThread)
        primary_thread_open = False
        boundary._close_handle(process_info.hProcess)
        creator_process_open = False

        if not set_event(barrier):
            raise ctypes.WinError(ctypes.get_last_error())
        if int(wait(safe_process, boundary._INFINITE)) != boundary._WAIT_OBJECT_0:
            raise RuntimeError("birth-protected release creator wait failed")
        exit_code = wintypes.DWORD(0)
        if not get_exit_code(safe_process, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(exit_code.value)
    finally:
        if creator_process_open:
            boundary._close_handle(process_info.hProcess)
        if primary_thread_open:
            boundary._close_handle(process_info.hThread)
        if _raw_handle_value(safe_process):
            boundary._close_handle(safe_process)
        boundary._close_handle(restricted_token)
        boundary._close_handle(barrier)
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(thread_descriptor)
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(process_descriptor)
        for name, value in old_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
