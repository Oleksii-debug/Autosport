$ErrorActionPreference = 'Stop'

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

# The creator fence needs one privileged handle-table census on Windows builds that
# redact kernel object identity from an unprivileged token. No release body is
# loaded while this bootstrap runs. If SeDebugPrivilege is assigned, enable it only
# after the creator DACL is installed, perform the census, and then irreversibly
# remove it in a finally block before the fence can return to release code.
$creatorPrivilegeBootstrapClassSource = @'
    public static class CreatorHostPrivilegeBootstrap
    {
        private const uint TOKEN_ADJUST_PRIVILEGES = 0x0020;
        private const uint TOKEN_QUERY = 0x0008;
        private const uint SE_PRIVILEGE_ENABLED = 0x00000002;
        private const uint SE_PRIVILEGE_REMOVED = 0x00000004;
        private const int ERROR_NOT_ALL_ASSIGNED = 1300;

        [StructLayout(LayoutKind.Sequential)]
        private struct Luid
        {
            public uint LowPart;
            public int HighPart;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LuidAndAttributes
        {
            public Luid Luid;
            public uint Attributes;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct TokenPrivileges
        {
            public uint PrivilegeCount;
            public LuidAndAttributes Privileges;
        }

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern void SetLastError(uint errorCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool OpenProcessToken(
            IntPtr processHandle,
            uint desiredAccess,
            out IntPtr tokenHandle);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool LookupPrivilegeValueW(
            string systemName,
            string name,
            out Luid luid);

        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AdjustTokenPrivileges(
            IntPtr tokenHandle,
            bool disableAllPrivileges,
            ref TokenPrivileges newState,
            uint bufferLength,
            IntPtr previousState,
            IntPtr returnLength);

        private static IntPtr OpenPrivilegeToken(out Luid luid)
        {
            IntPtr token;
            if (!OpenProcessToken(
                    GetCurrentProcess(),
                    TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                    out token))
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "creator-host token cannot be opened for SeDebugPrivilege control");
            }
            try
            {
                if (!LookupPrivilegeValueW(null, "SeDebugPrivilege", out luid))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "creator-host SeDebugPrivilege lookup failed");
                }
                return token;
            }
            catch
            {
                CloseHandle(token);
                throw;
            }
        }

        public static void EnableSeDebugPrivilegeForCensusIfAssigned()
        {
            Luid luid;
            IntPtr token = OpenPrivilegeToken(out luid);
            try
            {
                TokenPrivileges state = new TokenPrivileges
                {
                    PrivilegeCount = 1,
                    Privileges = new LuidAndAttributes
                    {
                        Luid = luid,
                        Attributes = SE_PRIVILEGE_ENABLED
                    }
                };
                SetLastError(0);
                if (!AdjustTokenPrivileges(
                        token,
                        false,
                        ref state,
                        0,
                        IntPtr.Zero,
                        IntPtr.Zero))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "creator-host SeDebugPrivilege census enable failed");
                }
                int error = Marshal.GetLastWin32Error();
                if (error == ERROR_NOT_ALL_ASSIGNED)
                {
                    Console.WriteLine("AUTOSPORT_CREATOR_SEDEBUG_CENSUS_AUTHORITY=ABSENT");
                    return;
                }
                if (error != 0)
                {
                    throw new Win32Exception(
                        error,
                        "creator-host SeDebugPrivilege census enable returned an ambiguous result");
                }
                Console.WriteLine("AUTOSPORT_CREATOR_SEDEBUG_CENSUS_AUTHORITY=ENABLED");
            }
            finally
            {
                CloseHandle(token);
            }
        }

        public static void RemoveSeDebugPrivilegeAndVerifyAbsent()
        {
            Luid luid;
            IntPtr token = OpenPrivilegeToken(out luid);
            try
            {
                TokenPrivileges removal = new TokenPrivileges
                {
                    PrivilegeCount = 1,
                    Privileges = new LuidAndAttributes
                    {
                        Luid = luid,
                        Attributes = SE_PRIVILEGE_REMOVED
                    }
                };
                SetLastError(0);
                if (!AdjustTokenPrivileges(
                        token,
                        false,
                        ref removal,
                        0,
                        IntPtr.Zero,
                        IntPtr.Zero))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "creator-host SeDebugPrivilege removal failed");
                }
                int removalError = Marshal.GetLastWin32Error();
                if (removalError != 0 && removalError != ERROR_NOT_ALL_ASSIGNED)
                {
                    throw new Win32Exception(
                        removalError,
                        "creator-host SeDebugPrivilege removal returned an ambiguous result");
                }

                // Assignment after SE_PRIVILEGE_REMOVED must fail with
                // ERROR_NOT_ALL_ASSIGNED. A success result proves the privilege is
                // still present and the process-DACL authority proof is unsafe.
                TokenPrivileges verification = new TokenPrivileges
                {
                    PrivilegeCount = 1,
                    Privileges = new LuidAndAttributes
                    {
                        Luid = luid,
                        Attributes = 0
                    }
                };
                SetLastError(0);
                if (!AdjustTokenPrivileges(
                        token,
                        false,
                        ref verification,
                        0,
                        IntPtr.Zero,
                        IntPtr.Zero))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "creator-host SeDebugPrivilege post-removal verification failed");
                }
                int verificationError = Marshal.GetLastWin32Error();
                if (verificationError == 0)
                {
                    throw new InvalidOperationException(
                        "creator-host SeDebugPrivilege remained assigned after irreversible removal attempt");
                }
                if (verificationError != ERROR_NOT_ALL_ASSIGNED)
                {
                    throw new Win32Exception(
                        verificationError,
                        "creator-host SeDebugPrivilege post-removal verification returned an ambiguous result");
                }
                Console.WriteLine("AUTOSPORT_CREATOR_SEDEBUG_REMOVAL=PASS");
            }
            finally
            {
                CloseHandle(token);
            }
        }
    }
'@

# Keep the existing creator-host fence byte-for-byte as a reviewed source input.
# Open it once with FileShare.Read (writers/deleters denied), bind its exact Git
# blob identity, then make deterministic fail-closed source substitutions:
# compile the privilege helper in the same C# source/assembly as the fence; defer
# the old pre-DACL "must be absent" assertion; bracket the existing handle census
# with temporary census authority and irreversible removal; and, on rejection only,
# expose bounded owner PID/process-name/access/handle diagnostics without accepting
# any authority that the reviewed fence rejected.
$creatorFenceBootstrapPath = Join-Path $PWD 'scripts/build_windows_creator_fence.ps1'
$expectedCreatorFenceBlob = '45623f3fed4d15af232f4cc0ab210abb16c16bef'
$creatorFenceStream = [System.IO.File]::Open(
  $creatorFenceBootstrapPath,
  [System.IO.FileMode]::Open,
  [System.IO.FileAccess]::Read,
  [System.IO.FileShare]::Read
)
try {
  $creatorFenceBuffer = [System.IO.MemoryStream]::new()
  try {
    $creatorFenceStream.CopyTo($creatorFenceBuffer)
    $creatorFenceBytes = $creatorFenceBuffer.ToArray()
  } finally {
    $creatorFenceBuffer.Dispose()
  }
  if ($creatorFenceBytes.Length -eq 0) {
    throw 'Creator-host fence bootstrap is empty'
  }

  $gitHeader = [System.Text.Encoding]::ASCII.GetBytes("blob $($creatorFenceBytes.Length)`0")
  $gitIdentityBytes = [System.IO.MemoryStream]::new()
  try {
    $gitIdentityBytes.Write($gitHeader, 0, $gitHeader.Length)
    $gitIdentityBytes.Write($creatorFenceBytes, 0, $creatorFenceBytes.Length)
    $gitIdentityBytes.Position = 0
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
      $actualCreatorFenceBlob = [System.Convert]::ToHexString(
        $sha1.ComputeHash($gitIdentityBytes)
      ).ToLowerInvariant()
    } finally {
      $sha1.Dispose()
    }
  } finally {
    $gitIdentityBytes.Dispose()
  }
  if ($actualCreatorFenceBlob -ne $expectedCreatorFenceBlob) {
    throw "Creator-host fence bootstrap Git blob mismatch: expected $expectedCreatorFenceBlob, got $actualCreatorFenceBlob"
  }

  $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
  $creatorFenceText = $strictUtf8.GetString($creatorFenceBytes)

  $oldClassAnchor = "namespace Autosport.Release`n{`n    public sealed class CreatorHostFence"
  $classAnchorMatches = [regex]::Matches(
    $creatorFenceText,
    [regex]::Escape($oldClassAnchor)
  ).Count
  if ($classAnchorMatches -ne 1) {
    throw "Creator-host fence expected exactly one C# class anchor; found $classAnchorMatches"
  }
  $newClassAnchor = "namespace Autosport.Release`n{`n" + $creatorPrivilegeBootstrapClassSource + "`n`n    public sealed class CreatorHostFence"
  $creatorFenceText = $creatorFenceText.Replace($oldClassAnchor, $newClassAnchor)

  $oldInitialGuard = "            RequireSeDebugNotAssigned();`n"
  $initialGuardMatches = [regex]::Matches(
    $creatorFenceText,
    [regex]::Escape($oldInitialGuard)
  ).Count
  if ($initialGuardMatches -ne 1) {
    throw "Creator-host fence expected exactly one initial SeDebug guard; found $initialGuardMatches"
  }
  $creatorFenceText = $creatorFenceText.Replace($oldInitialGuard, '')

  $oldCensus = "                RequireNoUntrustedPreexistingAuthority();`n"
  $censusMatches = [regex]::Matches(
    $creatorFenceText,
    [regex]::Escape($oldCensus)
  ).Count
  if ($censusMatches -ne 1) {
    throw "Creator-host fence expected exactly one pre-existing authority census; found $censusMatches"
  }
  $newCensus = @(
    '                CreatorHostPrivilegeBootstrap.EnableSeDebugPrivilegeForCensusIfAssigned();',
    '                try',
    '                {',
    '                    RequireNoUntrustedPreexistingAuthority();',
    '                }',
    '                finally',
    '                {',
    '                    CreatorHostPrivilegeBootstrap.RemoveSeDebugPrivilegeAndVerifyAbsent();',
    '                }',
    '                RequireSeDebugNotAssigned();'
  ) -join "`n"
  $newCensus += "`n"
  $creatorFenceText = $creatorFenceText.Replace($oldCensus, $newCensus)

  if ($releaseCreatorBirthProtected) {
    $protectedCreatorCensus = "                RequireSeDebugNotAssigned();`n"
    $injectedCreatorCensusMatches = [regex]::Matches($creatorFenceText, [regex]::Escape($newCensus)).Count
    if ($injectedCreatorCensusMatches -ne 1) { throw "Birth-protected creator expected exactly one injected late census; found $injectedCreatorCensusMatches" }
    $creatorFenceText = $creatorFenceText.Replace($newCensus, $protectedCreatorCensus)
  }

  $oldCompetingDeclaration = "            int competing = 0;`n"
  $competingDeclarationMatches = [regex]::Matches(
    $creatorFenceText,
    [regex]::Escape($oldCompetingDeclaration)
  ).Count
  if ($competingDeclarationMatches -ne 1) {
    throw "Creator-host fence expected exactly one competing-handle declaration; found $competingDeclarationMatches"
  }
  $newCompetingDeclaration = "            int competing = 0;`n            string competingDetails = `"`";`n"
  $creatorFenceText = $creatorFenceText.Replace($oldCompetingDeclaration, $newCompetingDeclaration)

  $oldCompetingIncrement = "                    competing++;`n"
  $competingIncrementMatches = [regex]::Matches(
    $creatorFenceText,
    [regex]::Escape($oldCompetingIncrement)
  ).Count
  if ($competingIncrementMatches -ne 1) {
    throw "Creator-host fence expected exactly one competing-handle increment; found $competingIncrementMatches"
  }
  $newCompetingIncrement = @(
    '                    if (competing < 32)',
    '                    {',
    '                        string ownerName = "<unavailable>";',
    '                        try',
    '                        {',
    '                            using (System.Diagnostics.Process ownerProcess =',
    '                                System.Diagnostics.Process.GetProcessById(checked((int)entry.UniqueProcessId.ToUInt64())))',
    '                            {',
    '                                ownerName = ownerProcess.ProcessName;',
    '                            }',
    '                        }',
    '                        catch',
    '                        {',
    '                        }',
    '                        competingDetails += String.Format(',
    '                            "{0}pid={1},name={2},access=0x{3:x8},handle=0x{4:x}",',
    '                            competingDetails.Length == 0 ? "" : ";",',
    '                            entry.UniqueProcessId.ToUInt64(),',
    '                            ownerName,',
    '                            entry.GrantedAccess,',
    '                            entry.HandleValue.ToUInt64());',
    '                    }',
    '                    competing++;'
  ) -join "`n"
  $newCompetingIncrement += "`n"
  $creatorFenceText = $creatorFenceText.Replace($oldCompetingIncrement, $newCompetingIncrement)

  $oldCompetingFailure = '                    String.Format("creator-host security fence found {0} pre-existing external dangerous process handle(s)", competing));'
  $competingFailureMatches = [regex]::Matches(
    $creatorFenceText,
    [regex]::Escape($oldCompetingFailure)
  ).Count
  if ($competingFailureMatches -ne 1) {
    throw "Creator-host fence expected exactly one competing-handle failure message; found $competingFailureMatches"
  }
  $newCompetingFailure = '                    String.Format("creator-host security fence found {0} pre-existing external dangerous process handle(s); owners=[{1}]", competing, competingDetails));'
  $creatorFenceText = $creatorFenceText.Replace($oldCompetingFailure, $newCompetingFailure)

  if ([regex]::Matches(
        $creatorFenceText,
        [regex]::Escape('public static class CreatorHostPrivilegeBootstrap')
      ).Count -ne 1) {
    throw 'Creator-host privilege helper injection was not unique'
  }
  $expectedCreatorCensusCalls = if ($releaseCreatorBirthProtected) { 0 } else { 1 }
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
  if ([regex]::Matches(
        $creatorFenceText,
        [regex]::Escape('pre-existing external dangerous process handle(s); owners=[')
      ).Count -ne 1) {
    throw 'Creator-host owner diagnostics injection was not unique'
  }

  $creatorFenceScriptBlock = [ScriptBlock]::Create($creatorFenceText)
  & $creatorFenceScriptBlock
} finally {
  $creatorFenceStream.Dispose()
}
