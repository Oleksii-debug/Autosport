from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_ORCHESTRATOR = _ROOT / "scripts" / "guarded_pyinstaller_orchestrator_boundary.cs"
_LAUNCH_BOUNDARY = _ROOT / "scripts" / "guarded_pyinstaller_launch_boundary.py"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell.exe")


def test_native_creator_is_fenced_before_release_sensitive_handles_exist() -> None:
    source = _ORCHESTRATOR.read_text(encoding="utf-8")
    run = source.split("public static int Run(", 1)[1]

    assert "private sealed class CreatorProcessFence : IDisposable" in source
    assert "SetEntriesInAclW(" in source
    assert "SetSecurityInfo(" in source
    assert "NtQuerySystemInformation(" in source
    assert "SYSTEM_EXTENDED_HANDLE_INFORMATION = 64" in source
    assert "RequireNoUntrustedPreexistingCreatorAuthority();" in source
    assert "(entry.GrantedAccess & DANGEROUS_PROCESS_ACCESS) != 0" in source
    assert "creatorFence.Acquire(currentUserSid, pythonExecutable);" in run
    assert run.index("creatorFence.Acquire(currentUserSid, pythonExecutable);") < run.index(
        "barrier = CreateEventW("
    )
    assert run.index("creatorFence.Acquire(currentUserSid, pythonExecutable);") < run.index(
        "CreateProcessAsUserW("
    )
    assert run.index("CreateProcessAsUserW(") < run.index(
        "RunHostileSiblingProbe(pythonExecutable, GetCurrentProcessId());"
    )
    assert run.index(
        "RunHostileSiblingProbe(pythonExecutable, GetCurrentProcessId());"
    ) < run.index("CloseHandle(processInfo.hThread)")
    assert run.index(
        "RunHostileSiblingProbe(pythonExecutable, GetCurrentProcessId());"
    ) < run.index("CloseHandle(processInfo.hProcess)")
    assert run.index("CloseHandle(processInfo.hProcess)") < run.index(
        "creatorFence.Dispose();"
    )


def test_creator_fence_preserves_the_existing_child_birth_boundaries() -> None:
    source = _ORCHESTRATOR.read_text(encoding="utf-8")

    assert "RequireSeDebugNotAssigned();" in source
    assert "CreateRestrictedToken(" in source
    assert "TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY" in source
    assert "RequireFreshProcessAccessDenied(processInfo.dwProcessId);" in source
    assert "RequireFreshThreadAccessDenied(processInfo.dwThreadId);" in source
    assert "RunHostileSiblingProbe(pythonExecutable, processInfo.dwProcessId);" in source
    assert "SetEvent(barrier)" in source
    assert "--autosport-birth-protected-worker" in source


@pytest.mark.skipif(
    os.name != "nt" or _powershell() is None,
    reason="real Windows creator PROCESS_DUP_HANDLE regression",
)
def test_preopened_external_process_dup_handle_fails_before_child_creation(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "hold_creator_dup_handle.py"
    helper.write_text(
        "import ctypes,sys\n"
        "from ctypes import wintypes\n"
        "PROCESS_DUP_HANDLE=0x40\n"
        "pid=int(sys.argv[1])\n"
        "k=ctypes.WinDLL('kernel32',use_last_error=True)\n"
        "k.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)\n"
        "k.OpenProcess.restype=wintypes.HANDLE\n"
        "k.CloseHandle.argtypes=(wintypes.HANDLE,)\n"
        "ctypes.set_last_error(0)\n"
        "h=k.OpenProcess(PROCESS_DUP_HANDLE,False,pid)\n"
        "v=h if isinstance(h,int) else ctypes.cast(h,ctypes.c_void_p).value\n"
        "if not v: raise SystemExit(f'OPEN_FAILED:{ctypes.get_last_error()}')\n"
        "print('READY',flush=True)\n"
        "try: sys.stdin.buffer.read(1)\n"
        "finally: k.CloseHandle(h)\n",
        encoding="utf-8",
    )
    binder = tmp_path / "must_not_run.py"
    marker = tmp_path / "binder-ran.txt"
    binder.write_text(
        "import pathlib,sys\n"
        "pathlib.Path(sys.argv[1]).write_text('RAN\\n',encoding='utf-8')\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "AUTOSPORT_TEST_BOUNDARY_CS": str(_ORCHESTRATOR),
            "AUTOSPORT_TEST_LAUNCH_BOUNDARY": str(_LAUNCH_BOUNDARY),
            "AUTOSPORT_TEST_BINDER": str(binder),
            "AUTOSPORT_TEST_MARKER": str(marker),
            "AUTOSPORT_TEST_HELPER": str(helper),
            "AUTOSPORT_TEST_PYTHON": sys.executable,
            "AUTOSPORT_TEST_WORKDIR": str(tmp_path),
        }
    )
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -Path $env:AUTOSPORT_TEST_BOUNDARY_CS
$info = [System.Diagnostics.ProcessStartInfo]::new()
$info.FileName = $env:AUTOSPORT_TEST_PYTHON
$info.UseShellExecute = $false
$info.RedirectStandardInput = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$info.CreateNoWindow = $true
$info.ArgumentList.Add('-I')
$info.ArgumentList.Add($env:AUTOSPORT_TEST_HELPER)
$info.ArgumentList.Add([string]$PID)
$helper = [System.Diagnostics.Process]::Start($info)
if ($null -eq $helper) { throw 'pre-open helper could not start' }
$ready = $helper.StandardOutput.ReadLine()
if ($ready -ne 'READY') {
  $stderr = $helper.StandardError.ReadToEnd()
  throw "pre-open helper failed: $ready $stderr"
}
$blocked = $false
try {
  $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $arguments = [string[]]@($env:AUTOSPORT_TEST_MARKER)
  try {
    [void][Autosport.Release.BirthProtectedPyInstaller]::Run(
      $env:AUTOSPORT_TEST_PYTHON,
      $env:AUTOSPORT_TEST_LAUNCH_BOUNDARY,
      $env:AUTOSPORT_TEST_BINDER,
      $arguments,
      $env:AUTOSPORT_TEST_WORKDIR,
      $sid
    )
  } catch {
    if ($_.Exception.ToString().Contains('pre-existing external dangerous process handle')) {
      $blocked = $true
    } else {
      throw
    }
  }
} finally {
  $helper.StandardInput.Write('x')
  $helper.StandardInput.Flush()
  $helper.StandardInput.Close()
  if (-not $helper.WaitForExit(5000)) {
    $helper.Kill()
    $helper.WaitForExit()
  }
  $helper.Dispose()
}
if (-not $blocked) { throw 'creator fence accepted a pre-opened PROCESS_DUP_HANDLE authority' }
"""
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert not marker.exists()
