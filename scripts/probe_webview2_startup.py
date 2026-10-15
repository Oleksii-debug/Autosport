"""Fail-closed, read-only native startup preflight for packaged Autosport on Windows.

This probe is NOT a semantic UI Automation, keyboard, STOP or human NVDA test.
A successful native preflight must be followed by the canonical external UIA gate.
Never print window titles, paths, browser versions, command lines or secrets.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Callable

_ALLOWED_TITLES = {
    "Автоспорт": "PRODUCT_MAIN_WINDOW",
    "Автоспорт — резервне аварійне керування": "NATIVE_EMERGENCY_STOP",
    "Автоспорт — помилка запуску": "NATIVE_STARTUP_ERROR",
}
_REQUIRED_FLAGS = ("real_money_execution", "human_tested", "nvda_verified", "whole_product_complete")


def classify_window_titles(titles: tuple[str, ...]) -> str:
    """Return only bounded classifications, never arbitrary window text."""
    classes = {_ALLOWED_TITLES.get(title) for title in titles}
    for name in ("NATIVE_STARTUP_ERROR", "NATIVE_EMERGENCY_STOP", "PRODUCT_MAIN_WINDOW"):
        if name in classes:
            return name
    return "OTHER_OR_UNAVAILABLE"


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        stat.S_ISREG(after.st_mode)
        and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
             before.st_ctime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns)
    )


def classify_witness(path: Path, *, started_ns: int) -> str:
    """Inspect only a fresh, held, bounded regular-file witness."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return "MISSING"
    except OSError:
        return "UNREADABLE"
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 4096:
        return "INVALID"
    if before.st_mtime_ns < started_ns:
        return "STALE"
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not _same_file_snapshot(before, opened):
            return "INVALID"
        raw = bytearray()
        while len(raw) <= 4096:
            chunk = os.read(fd, 4097 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        path_after = path.lstat()
        if (len(raw) != before.st_size
                or not _same_file_snapshot(before, after)
                or not _same_file_snapshot(before, path_after)):
            return "INVALID"
        data = json.loads(bytes(raw).decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return "INVALID"
    finally:
        if fd is not None:
            os.close(fd)
    if type(data) is not dict:
        return "INVALID"
    version = data.get("browser_version_string")
    if (
        type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or data.get("renderer") != "edgechromium"
        or data.get("observation_source") != "native_core_webview2_environment"
        or type(version) is not str
        or not version
        or len(version) > 256
        or version != version.strip()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in version)
        or any(data.get(key) is not False for key in _REQUIRED_FLAGS)
    ):
        return "INVALID"
    return "FRESH_UNQUALIFIED"


def _windows_process_alive(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    handle = kernel.OpenProcess(0x1000, 0, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such process
            return False
        raise OSError(error, "OpenProcess unavailable")
    try:
        code = ctypes.c_uint32(0)
        # STILL_ACTIVE=259 alone is ambiguous; an unsignaled handle is needed.
        wait_result = kernel.WaitForSingleObject(handle, 0)
        if wait_result == 0:
            return False
        if wait_result != 0x102:
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject unavailable")
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess unavailable")
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _windows_window_titles(pid: int) -> tuple[str, ...]:
    """Enumerate native top-level windows; never call blocking UIA COM."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = ctypes.c_int
    titles: list[str] = []

    @callback_type
    def collect(hwnd: int, _unused: int) -> int:
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, len(buf))
            titles.append(buf.value)
        return 1

    if not user32.EnumWindows(collect, 0):
        raise OSError(ctypes.get_last_error(), "EnumWindows unavailable")
    return tuple(titles)


def probe(
    workspace: Path,
    *,
    timeout_seconds: float,
    alive: Callable[[], bool],
    titles: Callable[[], tuple[str, ...]],
    started_ns: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    if (not workspace.is_absolute() or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 120):
        raise ValueError("workspace must be absolute and timeout must be 1..120 seconds")
    started_ns = time.time_ns() if started_ns is None else started_ns
    deadline = monotonic() + timeout_seconds
    window_class = "OTHER_OR_UNAVAILABLE"
    witness_state = "MISSING"
    while True:
        try:
            running = alive()
        except (OSError, RuntimeError):
            status = "PROCESS_STATUS_UNAVAILABLE"
            break
        if not running:
            status = "PROCESS_NOT_RUNNING"
            break
        try:
            window_class = classify_window_titles(titles())
        except (OSError, RuntimeError):
            status = "NATIVE_WINDOW_ENUMERATION_FAILED"
            break
        witness_state = classify_witness(workspace / "webview2-runtime-witness.json", started_ns=started_ns)
        if window_class in {"NATIVE_STARTUP_ERROR", "NATIVE_EMERGENCY_STOP"}:
            status = "BLOCKED_NATIVE_FALLBACK"
            break
        if witness_state in {"INVALID", "STALE", "UNREADABLE"}:
            status = "BLOCKED_RUNTIME_WITNESS"
            break
        if witness_state == "FRESH_UNQUALIFIED" and window_class == "PRODUCT_MAIN_WINDOW":
            status = "READY_FOR_SEMANTIC_UIA_NOT_PASS"
            break
        if monotonic() >= deadline:
            status = ("MAIN_WINDOW_MISSING_TIMEOUT" if witness_state == "FRESH_UNQUALIFIED"
                      else "WITNESS_MISSING_TIMEOUT")
            break
        sleep(0.1)
    return {
        "schema_version": 1,
        "status": status,
        "native_window_class": window_class,
        "runtime_witness_state": witness_state,
        "semantic_uia_verified": False,
        "human_tested": False,
        "nvda_verified": False,
        "real_money_execution": False,
        "whole_product_complete": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    parser.add_argument("--launch-epoch-ns", type=int, required=True,
                        help="time.time_ns() captured BEFORE launching the package")
    args = parser.parse_args()
    if (os.name != "nt" or args.pid <= 0 or not args.output.is_absolute()
            or args.launch_epoch_ns <= 0 or args.launch_epoch_ns > time.time_ns()):
        parser.error("Windows, positive PID, absolute output and pre-launch timestamp required")
    result = probe(
        args.workspace,
        timeout_seconds=args.timeout_seconds,
        alive=lambda: _windows_process_alive(args.pid),
        titles=lambda: _windows_window_titles(args.pid),
        started_ns=args.launch_epoch_ns,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".autosport-startup-", dir=args.output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, args.output)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return 0 if result["status"] == "READY_FOR_SEMANTIC_UIA_NOT_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
