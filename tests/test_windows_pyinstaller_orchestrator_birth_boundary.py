from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"
_ORCHESTRATOR_BOUNDARY = (
    _ROOT / "scripts" / "guarded_pyinstaller_orchestrator_boundary.cs"
)
_LAUNCH_BOUNDARY = _ROOT / "scripts" / "guarded_pyinstaller_launch_boundary.py"


def test_release_build_moves_guarded_binder_creation_to_trusted_orchestrator() -> None:
    build = _BUILD_SCRIPT.read_text(encoding="utf-8")
    launcher = _ORCHESTRATOR_BOUNDARY.read_text(encoding="utf-8")
    boundary = _LAUNCH_BOUNDARY.read_text(encoding="utf-8")

    assert "scripts/guarded_pyinstaller_orchestrator_boundary.cs" in build
    assert "scripts/guarded_pyinstaller_launch_boundary.py" in build
    assert build.count("[Autosport.Release.BirthProtectedPyInstaller]::Run(") == 2
    assert "& $packagingPython -I $trustedPyInstallerBinder" not in build
    assert "Add-Type -Path $trustedPyInstallerOrchestratorBoundary" in build
    assert build.index(
        "$trustedBuildManifestJson | & $pythonExecutable -I -S -c "
        "$trustedSourceSnapshotVerifierLauncher $trustedBuildRoot"
    ) < build.index("Add-Type -Path $trustedPyInstallerOrchestratorBoundary")
    assert build.index("Add-Type -Path $trustedPyInstallerOrchestratorBoundary") < build.index(
        "[Autosport.Release.BirthProtectedPyInstaller]::Run("
    )

    assert "private const uint DANGEROUS_PROCESS_ACCESS = 0x000C006A;" in launcher
    assert "private const uint DANGEROUS_THREAD_ACCESS = 0x000C17B3;" in launcher
    assert "RequireSeDebugNotAssigned();" in launcher
    assert "CreateRestrictedToken(" in launcher
    assert "Attributes = 0" in launcher
    assert "CreateProcessAsUserW(" in launcher
    assert "RequireFreshProcessAccessDenied(processInfo.dwProcessId);" in launcher
    assert "RequireFreshThreadAccessDenied(processInfo.dwThreadId);" in launcher
    assert "RunHostileSiblingProbe(pythonExecutable, processInfo.dwProcessId);" in launcher
    assert "CloseHandle(processInfo.hThread)" in launcher
    assert "CloseHandle(processInfo.hProcess)" in launcher
    assert "SetEvent(barrier)" in launcher

    run = launcher.split("public static int Run(", 1)[1]
    assert run.index("RequireSeDebugNotAssigned();") < run.index(
        "CreateRestrictedPrimaryToken(currentUserSid)"
    )
    assert run.index("CreateRestrictedPrimaryToken(currentUserSid)") < run.index(
        "CreateProcessAsUserW("
    )
    assert run.index("CreateProcessAsUserW(") < run.index(
        "RequireFreshProcessAccessDenied(processInfo.dwProcessId);"
    )
    assert run.index(
        "RequireFreshProcessAccessDenied(processInfo.dwProcessId);"
    ) < run.index("CloseHandle(processInfo.hProcess)")
    assert run.index("CloseHandle(processInfo.hThread)") < run.index("SetEvent(barrier)")
    assert run.index("CloseHandle(processInfo.hProcess)") < run.index("SetEvent(barrier)")

    # The native creator executes the existing exact-source launch-boundary file
    # directly. No creator-memory python -c bootstrap may redefine the child code.
    assert "_PROTECTED_BOOTSTRAP" not in launcher
    assert "_PROTECTED_BOOTSTRAP" not in boundary
    assert 'values.Add(launchBoundary);' in launcher
    assert 'values.Add(ProtectedWorkerArgument);' in launcher
    assert 'values.Add(binder);' in launcher
    assert "--autosport-birth-protected-worker" in launcher
    assert "--autosport-birth-protected-worker" in boundary
    assert "runpy.run_path(str(binder), run_name=\"__main__\")" in boundary
    assert "AUTOSPORT_BINDER_LAUNCH_BARRIER" in launcher
    assert "AUTOSPORT_BINDER_LAUNCH_NONCE" in launcher


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell.exe")


@pytest.mark.skipif(
    os.name != "nt" or _powershell() is None,
    reason="real Windows orchestrator birth-boundary regression",
)
def test_real_windows_orchestrator_denies_hostile_prebarrier_process_authority(
    tmp_path: Path,
) -> None:
    output = tmp_path / "trusted-worker-reached.txt"
    probe = tmp_path / "protected_probe.py"
    probe.write_text(
        "import ctypes,pathlib,sys\n"
        "from ctypes import wintypes\n"
        "marker=sys.modules.get('_autosport_birth_protected_worker')\n"
        "nonce=getattr(marker,'nonce',None)\n"
        "if not isinstance(nonce,str) or len(nonce)<32:\n"
        "    raise SystemExit('birth-protected marker missing')\n"
        "k=ctypes.WinDLL('kernel32',use_last_error=True)\n"
        "a=ctypes.WinDLL('advapi32',use_last_error=True)\n"
        "k.GetCurrentProcess.restype=wintypes.HANDLE\n"
        "a.OpenProcessToken.argtypes=(wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE))\n"
        "a.IsTokenRestricted.argtypes=(wintypes.HANDLE,)\n"
        "a.IsTokenRestricted.restype=wintypes.BOOL\n"
        "k.CloseHandle.argtypes=(wintypes.HANDLE,)\n"
        "token=wintypes.HANDLE()\n"
        "if not a.OpenProcessToken(k.GetCurrentProcess(),0x8,ctypes.byref(token)):\n"
        "    raise SystemExit(f'token open failed: {ctypes.get_last_error()}')\n"
        "try:\n"
        "    if not a.IsTokenRestricted(token):\n"
        "        raise SystemExit('worker token is not restricted')\n"
        "finally:\n"
        "    k.CloseHandle(token)\n"
        "pathlib.Path(sys.argv[1]).write_text('REACHED\\n',encoding='utf-8')\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "AUTOSPORT_TEST_BOUNDARY_CS": str(_ORCHESTRATOR_BOUNDARY),
            "AUTOSPORT_TEST_LAUNCH_BOUNDARY": str(_LAUNCH_BOUNDARY),
            "AUTOSPORT_TEST_BINDER": str(probe),
            "AUTOSPORT_TEST_OUTPUT": str(output),
            "AUTOSPORT_TEST_PYTHON": sys.executable,
            "AUTOSPORT_TEST_WORKDIR": str(tmp_path),
            "AUTOSPORT_TEST_ORCHESTRATOR_BIRTH_PROCESS_SIBLING_PROBE": "1",
        }
    )
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -Path $env:AUTOSPORT_TEST_BOUNDARY_CS
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$arguments = [string[]]@($env:AUTOSPORT_TEST_OUTPUT)
$exitCode = [Autosport.Release.BirthProtectedPyInstaller]::Run(
  $env:AUTOSPORT_TEST_PYTHON,
  $env:AUTOSPORT_TEST_LAUNCH_BOUNDARY,
  $env:AUTOSPORT_TEST_BINDER,
  $arguments,
  $env:AUTOSPORT_TEST_WORKDIR,
  $sid
)
exit $exitCode
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
    assert output.read_text(encoding="utf-8") == "REACHED\n"
