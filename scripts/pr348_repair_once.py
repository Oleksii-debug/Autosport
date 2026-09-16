from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


launcher = '''from __future__ import annotations

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

    barrier_name = "Local\\AutosportReleaseCreator-" + uuid.uuid4().hex
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
'''

launcher_path = ROOT / "scripts" / "guarded_release_creator_launch.py"
launcher_path.write_text(launcher, encoding="utf-8", newline="\n")

build_path = ROOT / "scripts" / "build_windows.ps1"
build = build_path.read_text(encoding="utf-8")
build_prefix = "$ErrorActionPreference = 'Stop'\n\n"
birth_prefix = '''$ErrorActionPreference = 'Stop'

# The workflow PowerShell is only a trusted launch root. The process that first
# executes release-sensitive creator logic is born behind process/thread deny
# DACLs with a restricted primary token, and remains behind a barrier until the
# launch root has closed its full creation-time process/thread handles.
$releaseCreatorPythonCommands = @(Get-Command python -CommandType Application -ErrorAction Stop)
if ($releaseCreatorPythonCommands.Count -lt 1) { throw 'Unable to resolve Python application for release-creator birth boundary' }
$releaseCreatorPython = [string]$releaseCreatorPythonCommands[0].Source
$releaseCreatorBirthProtected = ([string]$env:AUTOSPORT_RELEASE_CREATOR_BIRTH_PROTECTED -eq '1')
if (-not $releaseCreatorBirthProtected) {
  $releaseCreatorLauncher = Join-Path $PSScriptRoot 'guarded_release_creator_launch.py'
  if (-not (Test-Path -LiteralPath $releaseCreatorLauncher -PathType Leaf)) { throw 'Release-creator birth launcher is missing' }
  $releaseCreatorPowerShell = [string](Get-Process -Id $PID).Path
  if ([string]::IsNullOrWhiteSpace($releaseCreatorPowerShell)) { throw 'Unable to resolve current PowerShell executable for release-creator birth boundary' }
  & $releaseCreatorPython -I $releaseCreatorLauncher `
    --powershell $releaseCreatorPowerShell `
    --script $MyInvocation.MyCommand.Path `
    --working-directory ([System.IO.Path]::GetFullPath([string]$PWD))
  if ($LASTEXITCODE -ne 0) { throw "Birth-protected release creator exited $LASTEXITCODE" }
  return
}

$releaseCreatorBarrierName = [string]$env:AUTOSPORT_RELEASE_CREATOR_BARRIER
$releaseCreatorNonce = [string]$env:AUTOSPORT_RELEASE_CREATOR_NONCE
if ([string]::IsNullOrWhiteSpace($releaseCreatorBarrierName) -or [string]::IsNullOrWhiteSpace($releaseCreatorNonce) -or $releaseCreatorNonce.Length -lt 32) { throw 'Birth-protected release creator attestation is incomplete' }
$releaseCreatorBarrier = [System.Threading.EventWaitHandle]::OpenExisting($releaseCreatorBarrierName)
try {
  if (-not $releaseCreatorBarrier.WaitOne(60000)) { throw 'Birth-protected release creator barrier timed out' }
} finally {
  $releaseCreatorBarrier.Dispose()
}
Remove-Item Env:AUTOSPORT_RELEASE_CREATOR_BARRIER -ErrorAction SilentlyContinue
Remove-Item Env:AUTOSPORT_RELEASE_CREATOR_NONCE -ErrorAction SilentlyContinue
Remove-Item Env:AUTOSPORT_RELEASE_CREATOR_BIRTH_PROTECTED -ErrorAction SilentlyContinue

$releaseCreatorAttestation = @'
import ctypes
import sys
from ctypes import wintypes
ERROR_ACCESS_DENIED=5
TOKEN_QUERY=0x0008
rights=(0x2,0x8,0x20,0x40,0x40000,0x80000)
pid=int(sys.argv[1])
k=ctypes.WinDLL("kernel32",use_last_error=True)
a=ctypes.WinDLL("advapi32",use_last_error=True)
k.GetCurrentProcess.restype=wintypes.HANDLE
k.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
k.OpenProcess.restype=wintypes.HANDLE
k.CloseHandle.argtypes=(wintypes.HANDLE,)
a.OpenProcessToken.argtypes=(wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE))
a.OpenProcessToken.restype=wintypes.BOOL
a.IsTokenRestricted.argtypes=(wintypes.HANDLE,)
a.IsTokenRestricted.restype=wintypes.BOOL
t=wintypes.HANDLE()
if not a.OpenProcessToken(k.GetCurrentProcess(),TOKEN_QUERY,ctypes.byref(t)): raise SystemExit(f"release creator token open failed: {ctypes.get_last_error()}")
try:
    if not a.IsTokenRestricted(t): raise SystemExit("release creator token is not restricted")
finally:
    k.CloseHandle(t)
for access in rights:
    ctypes.set_last_error(0); h=k.OpenProcess(access,False,pid); v=h if isinstance(h,int) else ctypes.cast(h,ctypes.c_void_p).value
    if v: k.CloseHandle(h); raise SystemExit(f"release creator dangerous access available: 0x{access:08x}")
    e=ctypes.get_last_error()
    if e!=ERROR_ACCESS_DENIED: raise SystemExit(f"release creator access probe 0x{access:08x} failed non-deny: {e}")
print("PASS",flush=True)
'@
$releaseCreatorAttestationOutput = @(& $releaseCreatorPython -I -c $releaseCreatorAttestation $PID)
if ($LASTEXITCODE -ne 0 -or $releaseCreatorAttestationOutput.Count -ne 1 -or [string]$releaseCreatorAttestationOutput[0] -ne 'PASS') { throw "Birth-protected release creator attestation failed: $($releaseCreatorAttestationOutput -join ' ')" }
Write-Output 'AUTOSPORT_RELEASE_CREATOR_BIRTH_BOUNDARY=PASS'

$releaseCreatorTestCoordination = [string]$env:AUTOSPORT_TEST_RELEASE_CREATOR_BIRTH_COORDINATION
if (-not [string]::IsNullOrWhiteSpace($releaseCreatorTestCoordination)) {
  [System.IO.File]::WriteAllText($releaseCreatorTestCoordination, "$PID`n", [System.Text.UTF8Encoding]::new($false))
  Write-Output 'AUTOSPORT_RELEASE_CREATOR_BIRTH_TEST_READY=PASS'
  $releaseCreatorTestRelease = $releaseCreatorTestCoordination + '.release'
  $releaseCreatorTestDeadline = [DateTime]::UtcNow.AddSeconds(30)
  while (-not (Test-Path -LiteralPath $releaseCreatorTestRelease -PathType Leaf)) {
    if ([DateTime]::UtcNow -ge $releaseCreatorTestDeadline) { throw 'Birth-protected release creator test timed out waiting for release signal' }
    Start-Sleep -Milliseconds 25
  }
  Write-Output 'AUTOSPORT_RELEASE_CREATOR_RELEASE_BODY_LOADED=false'
  return
}

'''
build = replace_once(build, build_prefix, birth_prefix, "build birth-prefix")

census_replace_anchor = "  $creatorFenceText = $creatorFenceText.Replace($oldCensus, $newCensus)\n\n"
census_replace_new = '''  $creatorFenceText = $creatorFenceText.Replace($oldCensus, $newCensus)

  if ($releaseCreatorBirthProtected) {
    $protectedCreatorCensus = "                RequireSeDebugNotAssigned();`n"
    $injectedCreatorCensusMatches = [regex]::Matches($creatorFenceText, [regex]::Escape($newCensus)).Count
    if ($injectedCreatorCensusMatches -ne 1) { throw "Birth-protected creator expected exactly one injected late census; found $injectedCreatorCensusMatches" }
    $creatorFenceText = $creatorFenceText.Replace($newCensus, $protectedCreatorCensus)
  }

'''
build = replace_once(build, census_replace_anchor, census_replace_new, "protected census replacement")

old_enable_validation = '''  if ([regex]::Matches(
        $creatorFenceText,
        [regex]::Escape('CreatorHostPrivilegeBootstrap.EnableSeDebugPrivilegeForCensusIfAssigned();')
      ).Count -ne 1) {
    throw 'Creator-host census authority injection was not unique'
  }
  if ([regex]::Matches(
        $creatorFenceText,
        [regex]::Escape('CreatorHostPrivilegeBootstrap.RemoveSeDebugPrivilegeAndVerifyAbsent();')
      ).Count -ne 1) {
    throw 'Creator-host SeDebug removal injection was not unique'
  }
'''
new_enable_validation = '''  $expectedCreatorCensusCalls = if ($releaseCreatorBirthProtected) { 0 } else { 1 }
  if ([regex]::Matches(
        $creatorFenceText,
        [regex]::Escape('CreatorHostPrivilegeBootstrap.EnableSeDebugPrivilegeForCensusIfAssigned();')
      ).Count -ne $expectedCreatorCensusCalls) {
    throw "Creator-host census authority injection count did not match mode: expected $expectedCreatorCensusCalls"
  }
  if ([regex]::Matches(
        $creatorFenceText,
        [regex]::Escape('CreatorHostPrivilegeBootstrap.RemoveSeDebugPrivilegeAndVerifyAbsent();')
      ).Count -ne $expectedCreatorCensusCalls) {
    throw "Creator-host SeDebug removal injection count did not match mode: expected $expectedCreatorCensusCalls"
  }
'''
build = replace_once(build, old_enable_validation, new_enable_validation, "census validation")
build_path.write_text(build, encoding="utf-8", newline="\n")

cs_path = ROOT / "scripts" / "guarded_pyinstaller_orchestrator_boundary.cs"
cs = cs_path.read_text(encoding="utf-8")
cs_helper_anchor = "        private static IntPtr CreateRestrictedPrimaryToken(string currentUserSid)\n"
cs_helper = '''        private static bool CurrentProcessTokenIsRestricted()
        {
            IntPtr token = IntPtr.Zero;
            if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, out token))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "orchestrator current token cannot be opened");
            }
            try { return IsTokenRestricted(token); }
            finally { CloseHandle(token); }
        }

        private static IntPtr CreateRestrictedPrimaryToken(string currentUserSid)
'''
cs = replace_once(cs, cs_helper_anchor, cs_helper, "restricted-current-token helper")
cs_run_old = '''            RequireSeDebugNotAssigned();
            CreatorProcessFence creatorFence = new CreatorProcessFence();
            creatorFence.Acquire(currentUserSid, pythonExecutable);

            string nonce =
'''
cs_run_new = '''            RequireSeDebugNotAssigned();
            CreatorProcessFence creatorFence = new CreatorProcessFence();
            if (CurrentProcessTokenIsRestricted())
            {
                RequireFreshProcessAccessDenied(GetCurrentProcessId());
            }
            else
            {
                creatorFence.Acquire(currentUserSid, pythonExecutable);
            }

            string nonce =
'''
cs = replace_once(cs, cs_run_old, cs_run_new, "orchestrator creator fence branch")
cs_path.write_text(cs, encoding="utf-8", newline="\n")

test_path = ROOT / "tests" / "test_windows_creator_host_bootstrap.py"
test_text = test_path.read_text(encoding="utf-8")
first_start = test_text.index("def test_creator_host_census_authority_is_removed_before_exact_release_body()")
decorator = test_text.index("\n@pytest.mark.skipif(", first_start)
new_first = '''def test_release_creator_is_birth_protected_before_release_sensitive_bootstrap() -> None:
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
    assert "creator-host build body blob is not the reviewed predecessor body" in fence_bootstrap
    assert "Add-Type -Path $trustedPyInstallerOrchestratorBoundary" in body
    assert body.count("[Autosport.Release.BirthProtectedPyInstaller]::Run(") == 2


'''
test_text = test_text[:first_start] + new_first + test_text[decorator + 1:]
second_start = test_text.index("@pytest.mark.skipif(")
new_second = '''@pytest.mark.skipif(
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
    assert launched.returncode == 0, launched.stdout + "\\n" + launched.stderr
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
        release.write_text("release\\n", encoding="utf-8")
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
'''
test_text = test_text[:second_start] + new_second + "\n"
test_path.write_text(test_text, encoding="utf-8", newline="\n")

orchestrator_test_path = ROOT / "tests" / "test_windows_pyinstaller_orchestrator_birth_boundary.py"
orchestrator_test = orchestrator_test_path.read_text(encoding="utf-8")
anchor = '''    assert "RequireSeDebugNotAssigned();" in launcher
    assert "CreateRestrictedToken(" in launcher
'''
replacement = '''    assert "RequireSeDebugNotAssigned();" in launcher
    assert "CurrentProcessTokenIsRestricted()" in launcher
    assert "RequireFreshProcessAccessDenied(GetCurrentProcessId());" in launcher
    assert "CreateRestrictedToken(" in launcher
'''
orchestrator_test = replace_once(orchestrator_test, anchor, replacement, "orchestrator restricted-current assertion")
orchestrator_test_path.write_text(orchestrator_test, encoding="utf-8", newline="\n")

print("PR348 birth-boundary repair patch applied")
