from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _ROOT / "scripts" / "build_windows.ps1"
_FENCE_BOOTSTRAP = _ROOT / "scripts" / "build_windows_creator_fence.ps1"
_BODY = _ROOT / "scripts" / "build_windows_body.ps1"
_REVIEWED_FENCE_BLOB = "45623f3fed4d15af232f4cc0ab210abb16c16bef"
_REVIEWED_BODY_BLOB = "6116d30fd70aa5c0f7f115b03fdf27170d2d53c4"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell.exe")


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(
        f"blob {len(data)}\0".encode("ascii") + data,
        usedforsecurity=False,
    ).hexdigest()


def test_release_creator_is_birth_protected_before_release_sensitive_bootstrap() -> None:
    bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
    fence_bootstrap = _FENCE_BOOTSTRAP.read_text(encoding="utf-8")
    body = _BODY.read_text(encoding="utf-8")
    launcher = (_ROOT / "scripts" / "guarded_release_creator_launch.py").read_text(encoding="utf-8")
    assert _git_blob_sha(_FENCE_BOOTSTRAP.read_bytes()) == _REVIEWED_FENCE_BLOB
    assert _git_blob_sha(_BODY.read_bytes()) == _REVIEWED_BODY_BLOB
    assert "guarded_release_creator_launch.py" in bootstrap
    assert "AUTOSPORT_RELEASE_CREATOR_BIRTH_PROTECTED" in bootstrap
    assert "AUTOSPORT_RELEASE_CREATOR_BIRTH_BOUNDARY=PASS" in bootstrap
    assert bootstrap.index("guarded_release_creator_launch.py") < bootstrap.index("$creatorPrivilegeBootstrapClassSource")
    assert "CreateProcessAsUserW" in launcher
    assert "_birth_security_descriptor" in launcher
    assert "_birth_thread_security_descriptor" in launcher
    assert "_create_restricted_primary_token" in launcher
    assert launcher.index("_require_fresh_process_access_denied") < launcher.index("set_event(barrier)")
    assert launcher.index("_close_handle(process_info.hThread)") < launcher.index("set_event(barrier)")
    assert launcher.index("_close_handle(process_info.hProcess)") < launcher.index("set_event(barrier)")
    assert "if ($releaseCreatorBirthProtected)" in bootstrap
    assert "$expectedCreatorCensusCalls = if ($releaseCreatorBirthProtected) { 0 } else { 1 }" in bootstrap
    assert "RequireNoUntrustedPreexistingAuthority();" in fence_bootstrap
    assert "RequireSeDebugNotAssigned();" in fence_bootstrap
    assert "AUTOSPORT_CREATOR_FENCE_FRESH_OPEN_DENIAL=PASS" in fence_bootstrap
    assert "build body blob is not the reviewed predecessor body" in fence_bootstrap
    assert "Add-Type -Path $trustedPyInstallerOrchestratorBoundary" in body
    assert body.count("[Autosport.Release.BirthProtectedPyInstaller]::Run(") == 2


@pytest.mark.skipif(
    os.name != "nt" or _powershell() is None,
    reason="real Windows release-creator birth-boundary regression",
)
def test_hostile_sibling_cannot_obtain_dangerous_release_creator_authority(tmp_path: Path) -> None:
    coordination = tmp_path / "release-creator-birth.coord"
    release = Path(str(coordination) + ".release")
    stdout_path = tmp_path / "release-creator.stdout.txt"
    stderr_path = tmp_path / "release-creator.stderr.txt"
    env = os.environ.copy()
    env["AUTOSPORT_TEST_RELEASE_CREATOR_BIRTH_COORDINATION"] = str(coordination)
    powershell = _powershell()
    assert powershell is not None
    command = (f'start "" /b "{powershell}" -NoProfile -NonInteractive ' f'-File "{_BOOTSTRAP}" >"{stdout_path}" 2>"{stderr_path}"')
    launched = subprocess.run(["cmd.exe", "/d", "/s", "/c", command], cwd=_ROOT, env=env, capture_output=True, text=True, timeout=15)
    assert launched.returncode == 0, launched.stdout + "\n" + launched.stderr
    deadline = time.monotonic() + 30
    while not coordination.exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert coordination.exists(), "protected creator did not publish test coordination"
    lines = coordination.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    pid = int(lines[0])
    error_access_denied = 5
    synchronize = 0x00100000
    process_query_limited_information = 0x00001000
    wait_object_0 = 0x00000000
    dangerous = (0x2, 0x8, 0x20, 0x40, 0x40000, 0x80000)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code_process.restype = wintypes.BOOL
    for access in dangerous:
        ctypes.set_last_error(0)
        hostile = open_process(access, False, pid)
        hostile_value = hostile if isinstance(hostile, int) else ctypes.cast(hostile, ctypes.c_void_p).value
        if hostile_value:
            close_handle(hostile)
        assert not hostile_value, f"dangerous OpenProcess 0x{access:08x} succeeded"
        assert ctypes.get_last_error() == error_access_denied
    safe = open_process(synchronize | process_query_limited_information, False, pid)
    safe_value = safe if isinstance(safe, int) else ctypes.cast(safe, ctypes.c_void_p).value
    assert safe_value, f"safe wait OpenProcess failed: {ctypes.get_last_error()}"
    try:
        release.write_text("release\n", encoding="utf-8")
        assert wait_for_single_object(safe, 30_000) == wait_object_0
        exit_code = wintypes.DWORD(0)
        assert get_exit_code_process(safe, ctypes.byref(exit_code))
        assert exit_code.value == 0
    finally:
        assert close_handle(safe)
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert "AUTOSPORT_RELEASE_CREATOR_BIRTH_BOUNDARY=PASS" in stdout, stderr
    assert "AUTOSPORT_RELEASE_CREATOR_BIRTH_TEST_READY=PASS" in stdout, stderr
    assert "AUTOSPORT_RELEASE_CREATOR_RELEASE_BODY_LOADED=false" in stdout, stderr

