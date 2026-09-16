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


def test_creator_host_census_authority_is_removed_before_exact_release_body() -> None:
    bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
    fence_bootstrap = _FENCE_BOOTSTRAP.read_text(encoding="utf-8")
    body = _BODY.read_text(encoding="utf-8")

    fence = "$creatorFence = [Autosport.Release.CreatorHostFence]::Acquire()"
    body_lookup = (
        "$bodyEntry = (& $gitExecutable ls-tree $sourceSha -- "
        "'scripts/build_windows_body.ps1').Trim()"
    )
    body_execute = "& $creatorBodyTemp"
    native_load = "Add-Type -Path $trustedPyInstallerOrchestratorBoundary"
    native_run = "[Autosport.Release.BirthProtectedPyInstaller]::Run("

    assert _git_blob_sha(_FENCE_BOOTSTRAP.read_bytes()) == _REVIEWED_FENCE_BLOB
    assert _git_blob_sha(_BODY.read_bytes()) == _REVIEWED_BODY_BLOB

    # A hosted runner may assign SeDebugPrivilege. It is enabled only for the
    # trusted, pre-release handle census and is irreversibly removed in finally.
    assert "private const uint SE_PRIVILEGE_ENABLED = 0x00000002;" in bootstrap
    assert "private const uint SE_PRIVILEGE_REMOVED = 0x00000004;" in bootstrap
    assert "private const int ERROR_NOT_ALL_ASSIGNED = 1300;" in bootstrap
    assert "EnableSeDebugPrivilegeForCensusIfAssigned" in bootstrap
    assert "Attributes = SE_PRIVILEGE_ENABLED" in bootstrap
    assert "Attributes = SE_PRIVILEGE_REMOVED" in bootstrap
    assert "CreatorHostPrivilegeBootstrap.EnableSeDebugPrivilegeForCensusIfAssigned();" in bootstrap
    assert "CreatorHostPrivilegeBootstrap.RemoveSeDebugPrivilegeAndVerifyAbsent();" in bootstrap
    assert "RequireSeDebugNotAssigned();" in bootstrap
    assert "AUTOSPORT_CREATOR_SEDEBUG_CENSUS_AUTHORITY=" in bootstrap
    assert "AUTOSPORT_CREATOR_SEDEBUG_REMOVAL=PASS" in bootstrap
    assert "SeDebugPrivilege remained assigned after irreversible removal attempt" in bootstrap

    # The previously reviewed fence remains the exact source input. The bootstrap
    # accepts only that Git blob and applies two unique, deterministic substitutions.
    assert f"$expectedCreatorFenceBlob = '{_REVIEWED_FENCE_BLOB}'" in bootstrap
    assert "[System.IO.FileShare]::Read" in bootstrap
    assert "$initialGuardMatches -ne 1" in bootstrap
    assert "$censusMatches -ne 1" in bootstrap
    assert "Creator-host census authority injection was not unique" in bootstrap
    assert "Creator-host SeDebug removal injection was not unique" in bootstrap
    assert "[ScriptBlock]::Create($creatorFenceText)" in bootstrap

    # The exact source fence still owns DACL installation, pre-existing-handle
    # census, fresh-open denial, and exact release-body loading.
    assert "private const uint DANGEROUS_PROCESS_ACCESS = 0x000C006A;" in fence_bootstrap
    assert "RequireNoUntrustedPreexistingAuthority();" in fence_bootstrap
    assert "RequireSeDebugNotAssigned();" in fence_bootstrap
    assert "NtQuerySystemInformation(" in fence_bootstrap
    assert "SetSecurityInfo(" in fence_bootstrap
    assert "AUTOSPORT_CREATOR_FENCE_FRESH_OPEN_DENIAL=PASS" in fence_bootstrap
    assert fence_bootstrap.index(fence) < fence_bootstrap.index(body_lookup) < fence_bootstrap.index(body_execute)
    assert native_load not in bootstrap
    assert native_run not in bootstrap
    assert native_load not in fence_bootstrap
    assert native_run not in fence_bootstrap
    assert native_load in body
    assert body.count(native_run) == 2
    assert "creator-host build body blob is not the reviewed predecessor body" in fence_bootstrap
    assert fence_bootstrap.index(body_execute) < fence_bootstrap.index("$creatorFence.Dispose()")


@pytest.mark.skipif(
    os.name != "nt" or _powershell() is None,
    reason="real Windows creator-host pre-census regression",
)
def test_used_then_closed_vm_write_authority_cannot_reach_release_body(tmp_path: Path) -> None:
    coordination = tmp_path / "creator-fence.coord"
    release = Path(str(coordination) + ".release")
    stdout_path = tmp_path / "creator-fence.stdout.txt"
    stderr_path = tmp_path / "creator-fence.stderr.txt"

    env = os.environ.copy()
    env["AUTOSPORT_TEST_CREATOR_FENCE_COORDINATION"] = str(coordination)
    powershell = _powershell()
    assert powershell is not None
    command = (
        f'start "" /b "{powershell}" -NoProfile -NonInteractive '
        f'-File "{_BOOTSTRAP}" >"{stdout_path}" 2>"{stderr_path}"'
    )
    launched = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert launched.returncode == 0, launched.stdout + "\n" + launched.stderr

    deadline = time.monotonic() + 30
    while not coordination.exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert coordination.exists(), "creator bootstrap did not publish the pre-fence canary"
    lines = coordination.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    pid = int(lines[0])
    address = int(lines[1])
    assert pid > 0 and address > 0

    process_vm_operation = 0x00000008
    process_vm_write = 0x00000020
    synchronize = 0x00100000
    process_query_limited_information = 0x00001000
    wait_object_0 = 0x00000000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    write_process_memory = kernel32.WriteProcessMemory
    write_process_memory.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    write_process_memory.restype = wintypes.BOOL
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code_process.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    hostile = open_process(process_vm_operation | process_vm_write, False, pid)
    hostile_value = hostile if isinstance(hostile, int) else ctypes.cast(hostile, ctypes.c_void_p).value
    assert hostile_value, f"pre-fence OpenProcess failed: {ctypes.get_last_error()}"
    try:
        sentinel = ctypes.c_longlong(0x0102030405060708)
        written = ctypes.c_size_t(0)
        assert write_process_memory(
            hostile,
            ctypes.c_void_p(address),
            ctypes.byref(sentinel),
            ctypes.sizeof(sentinel),
            ctypes.byref(written),
        ), f"WriteProcessMemory failed: {ctypes.get_last_error()}"
        assert written.value == ctypes.sizeof(sentinel)
    finally:
        assert close_handle(hostile)

    # The dangerous handle is now gone. A late point-in-time census alone cannot
    # observe that it already performed a durable mutation. Release the bootstrap:
    # the production fence must be acquired before any release body is loaded, and
    # must make every subsequent fresh dangerous open fail closed.
    release.write_text("release\n", encoding="utf-8")

    ctypes.set_last_error(0)
    safe = open_process(synchronize | process_query_limited_information, False, pid)
    safe_value = safe if isinstance(safe, int) else ctypes.cast(safe, ctypes.c_void_p).value
    assert safe_value, f"safe wait OpenProcess failed: {ctypes.get_last_error()}"
    try:
        assert wait_for_single_object(safe, 30_000) == wait_object_0
        exit_code = wintypes.DWORD(0)
        assert get_exit_code_process(safe, ctypes.byref(exit_code))
        assert exit_code.value == 0
    finally:
        assert close_handle(safe)

    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert "AUTOSPORT_CREATOR_SEDEBUG_CENSUS_AUTHORITY=" in stdout, stderr
    assert "AUTOSPORT_CREATOR_SEDEBUG_REMOVAL=PASS" in stdout, stderr
    assert "AUTOSPORT_CREATOR_PRE_FENCE_MUTATION_OBSERVED=PASS" in stdout, stderr
    assert "AUTOSPORT_CREATOR_FENCE_FRESH_OPEN_DENIAL=PASS" in stdout, stderr
    assert "AUTOSPORT_CREATOR_RELEASE_BODY_LOADED=false" in stdout, stderr
