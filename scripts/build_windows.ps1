$ErrorActionPreference = 'Stop'

# This bootstrap exists only to establish creator-host process authority before the
# release body creates any PyInstaller/orchestrator state. The release body remains
# the exact previously reviewed Git blob and is materialized from the selected
# source commit only after this fence is installed and audited.
$creatorFenceSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;

namespace Autosport.Release
{
    public sealed class CreatorHostFence : IDisposable
    {
        private const uint PROCESS_CREATE_THREAD = 0x00000002;
        private const uint PROCESS_VM_OPERATION = 0x00000008;
        private const uint PROCESS_VM_WRITE = 0x00000020;
        private const uint PROCESS_DUP_HANDLE = 0x00000040;
        private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000;
        private const uint READ_CONTROL = 0x00020000;
        private const uint WRITE_DAC = 0x00040000;
        private const uint WRITE_OWNER = 0x00080000;
        private const uint DANGEROUS_PROCESS_ACCESS = 0x000C006A;
        private const uint TOKEN_ADJUST_PRIVILEGES = 0x0020;
        private const uint TOKEN_QUERY = 0x0008;
        private const int ERROR_NOT_ALL_ASSIGNED = 1300;
        private const uint SE_KERNEL_OBJECT = 6;
        private const uint DACL_SECURITY_INFORMATION = 0x00000004;
        private const int DENY_ACCESS = 3;
        private const uint NO_INHERITANCE = 0;
        private const int NO_MULTIPLE_TRUSTEE = 0;
        private const int TRUSTEE_IS_SID = 0;
        private const int TRUSTEE_IS_UNKNOWN = 0;
        private const int SYSTEM_EXTENDED_HANDLE_INFORMATION = 64;
        private const uint STATUS_INFO_LENGTH_MISMATCH = 0xC0000004;
        private const int MAX_SYSTEM_HANDLE_SNAPSHOT_BYTES = 64 * 1024 * 1024;
        private const string OWNER_RIGHTS_SID = "S-1-3-4";

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

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct Trustee
        {
            public IntPtr pMultipleTrustee;
            public int MultipleTrusteeOperation;
            public int TrusteeForm;
            public int TrusteeType;
            public IntPtr ptstrName;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct ExplicitAccess
        {
            public uint grfAccessPermissions;
            public int grfAccessMode;
            public uint grfInheritance;
            public Trustee Trustee;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SystemHandleTableEntryInfoEx
        {
            public IntPtr Object;
            public UIntPtr UniqueProcessId;
            public UIntPtr HandleValue;
            public uint GrantedAccess;
            public ushort CreatorBackTraceIndex;
            public ushort ObjectTypeIndex;
            public uint HandleAttributes;
            public uint Reserved;
        }

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentProcessId();

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, uint processId);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr LocalFree(IntPtr memory);

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

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ConvertStringSidToSidW(string stringSid, out IntPtr sid);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint GetSecurityInfo(
            IntPtr handle,
            uint objectType,
            uint securityInfo,
            IntPtr owner,
            IntPtr group,
            out IntPtr dacl,
            IntPtr sacl,
            out IntPtr securityDescriptor);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint SetEntriesInAclW(
            uint countOfExplicitEntries,
            [In] ExplicitAccess[] explicitEntries,
            IntPtr oldAcl,
            out IntPtr newAcl);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint SetSecurityInfo(
            IntPtr handle,
            uint objectType,
            uint securityInfo,
            IntPtr owner,
            IntPtr group,
            IntPtr dacl,
            IntPtr sacl);

        [DllImport("ntdll.dll")]
        private static extern int NtQuerySystemInformation(
            int systemInformationClass,
            IntPtr systemInformation,
            uint systemInformationLength,
            out uint returnLength);

        private IntPtr originalSecurityDescriptor;
        private IntPtr originalDacl;
        private bool installed;

        private CreatorHostFence()
        {
        }

        public static CreatorHostFence Acquire()
        {
            RequireSeDebugNotAssigned();
            string currentUserSid = WindowsIdentity.GetCurrent().User.Value;
            if (String.IsNullOrWhiteSpace(currentUserSid) || !currentUserSid.StartsWith("S-", StringComparison.Ordinal))
            {
                throw new InvalidOperationException("creator-host fence cannot resolve a canonical current-user SID");
            }

            IntPtr currentProcess = GetCurrentProcess();
            IntPtr dacl = IntPtr.Zero;
            IntPtr securityDescriptor = IntPtr.Zero;
            uint securityError = GetSecurityInfo(
                currentProcess,
                SE_KERNEL_OBJECT,
                DACL_SECURITY_INFORMATION,
                IntPtr.Zero,
                IntPtr.Zero,
                out dacl,
                IntPtr.Zero,
                out securityDescriptor);
            if (securityError != 0)
            {
                throw new Win32Exception(unchecked((int)securityError), "creator-host DACL capture failed");
            }
            if (securityDescriptor == IntPtr.Zero || dacl == IntPtr.Zero)
            {
                if (securityDescriptor != IntPtr.Zero)
                {
                    LocalFree(securityDescriptor);
                }
                throw new InvalidOperationException("creator-host DACL is unavailable or NULL");
            }

            IntPtr currentUser = IntPtr.Zero;
            IntPtr ownerRights = IntPtr.Zero;
            IntPtr newAcl = IntPtr.Zero;
            bool daclInstalled = false;
            try
            {
                if (!ConvertStringSidToSidW(currentUserSid, out currentUser))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "creator-host current-user SID conversion failed");
                }
                if (!ConvertStringSidToSidW(OWNER_RIGHTS_SID, out ownerRights))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "creator-host OWNER RIGHTS SID conversion failed");
                }

                ExplicitAccess[] entries = new ExplicitAccess[]
                {
                    DenyEntry(currentUser),
                    DenyEntry(ownerRights)
                };
                uint aclError = SetEntriesInAclW(2, entries, dacl, out newAcl);
                if (aclError != 0)
                {
                    throw new Win32Exception(unchecked((int)aclError), "creator-host deny ACL construction failed");
                }
                uint setError = SetSecurityInfo(
                    currentProcess,
                    SE_KERNEL_OBJECT,
                    DACL_SECURITY_INFORMATION,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    newAcl,
                    IntPtr.Zero);
                if (setError != 0)
                {
                    throw new Win32Exception(unchecked((int)setError), "creator-host deny DACL installation failed");
                }
                daclInstalled = true;

                RequireNoUntrustedPreexistingAuthority();

                CreatorHostFence fence = new CreatorHostFence();
                fence.originalSecurityDescriptor = securityDescriptor;
                fence.originalDacl = dacl;
                fence.installed = true;
                securityDescriptor = IntPtr.Zero;
                return fence;
            }
            catch
            {
                if (daclInstalled)
                {
                    uint restoreError = SetSecurityInfo(
                        currentProcess,
                        SE_KERNEL_OBJECT,
                        DACL_SECURITY_INFORMATION,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        dacl,
                        IntPtr.Zero);
                    if (restoreError != 0)
                    {
                        throw new Win32Exception(
                            unchecked((int)restoreError),
                            "creator-host DACL restoration failed after fence rejection");
                    }
                }
                throw;
            }
            finally
            {
                if (newAcl != IntPtr.Zero)
                {
                    LocalFree(newAcl);
                }
                if (ownerRights != IntPtr.Zero)
                {
                    LocalFree(ownerRights);
                }
                if (currentUser != IntPtr.Zero)
                {
                    LocalFree(currentUser);
                }
                if (securityDescriptor != IntPtr.Zero)
                {
                    LocalFree(securityDescriptor);
                }
            }
        }

        public void Dispose()
        {
            if (!installed)
            {
                return;
            }
            try
            {
                uint error = SetSecurityInfo(
                    GetCurrentProcess(),
                    SE_KERNEL_OBJECT,
                    DACL_SECURITY_INFORMATION,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    originalDacl,
                    IntPtr.Zero);
                if (error != 0)
                {
                    throw new Win32Exception(unchecked((int)error), "creator-host DACL restoration failed");
                }
            }
            finally
            {
                if (originalSecurityDescriptor != IntPtr.Zero)
                {
                    LocalFree(originalSecurityDescriptor);
                }
                originalSecurityDescriptor = IntPtr.Zero;
                originalDacl = IntPtr.Zero;
                installed = false;
            }
        }

        private static ExplicitAccess DenyEntry(IntPtr sid)
        {
            return new ExplicitAccess
            {
                grfAccessPermissions = DANGEROUS_PROCESS_ACCESS,
                grfAccessMode = DENY_ACCESS,
                grfInheritance = NO_INHERITANCE,
                Trustee = new Trustee
                {
                    pMultipleTrustee = IntPtr.Zero,
                    MultipleTrusteeOperation = NO_MULTIPLE_TRUSTEE,
                    TrusteeForm = TRUSTEE_IS_SID,
                    TrusteeType = TRUSTEE_IS_UNKNOWN,
                    ptstrName = sid
                }
            };
        }

        private static void RequireSeDebugNotAssigned()
        {
            IntPtr token = IntPtr.Zero;
            if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, out token))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "creator-host token cannot be inspected for SeDebugPrivilege");
            }
            try
            {
                Luid luid;
                if (!LookupPrivilegeValueW(null, "SeDebugPrivilege", out luid))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "creator-host SeDebugPrivilege lookup failed");
                }
                TokenPrivileges state = new TokenPrivileges
                {
                    PrivilegeCount = 1,
                    Privileges = new LuidAndAttributes { Luid = luid, Attributes = 0 }
                };
                Marshal.GetLastWin32Error();
                if (!AdjustTokenPrivileges(token, false, ref state, 0, IntPtr.Zero, IntPtr.Zero))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "creator-host SeDebugPrivilege assignment probe failed");
                }
                int error = Marshal.GetLastWin32Error();
                if (error == 0)
                {
                    throw new InvalidOperationException("creator-host token has SeDebugPrivilege assigned; process-DACL proof is not authoritative");
                }
                if (error != ERROR_NOT_ALL_ASSIGNED)
                {
                    throw new Win32Exception(error, "creator-host SeDebugPrivilege assignment probe returned an ambiguous result");
                }
            }
            finally
            {
                CloseHandle(token);
            }
        }

        private static void RequireNoUntrustedPreexistingAuthority()
        {
            uint currentPid = GetCurrentProcessId();
            IntPtr identityHandle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, currentPid);
            if (identityHandle == IntPtr.Zero)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "creator-host identity handle cannot be opened");
            }
            try
            {
                int size = 1024 * 1024;
                while (size <= MAX_SYSTEM_HANDLE_SNAPSHOT_BYTES)
                {
                    IntPtr buffer = IntPtr.Zero;
                    try
                    {
                        buffer = Marshal.AllocHGlobal(size);
                        uint returned;
                        int status = NtQuerySystemInformation(
                            SYSTEM_EXTENDED_HANDLE_INFORMATION,
                            buffer,
                            unchecked((uint)size),
                            out returned);
                        uint statusCode = unchecked((uint)status);
                        if (statusCode == 0)
                        {
                            AuditSnapshot(buffer, size, currentPid, identityHandle);
                            return;
                        }
                        if (statusCode != STATUS_INFO_LENGTH_MISMATCH)
                        {
                            throw new InvalidOperationException(
                                String.Format("creator-host handle census failed: NTSTATUS=0x{0:x8}", statusCode));
                        }
                        long requested = Math.Max((long)size * 2, (long)returned + 64 * 1024);
                        if (requested > MAX_SYSTEM_HANDLE_SNAPSHOT_BYTES)
                        {
                            break;
                        }
                        size = checked((int)requested);
                    }
                    finally
                    {
                        if (buffer != IntPtr.Zero)
                        {
                            Marshal.FreeHGlobal(buffer);
                        }
                    }
                }
                throw new InvalidOperationException("creator-host handle census exceeded bounded capture size");
            }
            finally
            {
                CloseHandle(identityHandle);
            }
        }

        private static void AuditSnapshot(IntPtr buffer, int bufferSize, uint currentPid, IntPtr identityHandle)
        {
            int headerSize = IntPtr.Size * 2;
            if (bufferSize < headerSize)
            {
                throw new InvalidOperationException("creator-host handle census header was truncated");
            }
            ulong count = IntPtr.Size == 8
                ? unchecked((ulong)Marshal.ReadInt64(buffer))
                : unchecked((uint)Marshal.ReadInt32(buffer));
            int entrySize = Marshal.SizeOf(typeof(SystemHandleTableEntryInfoEx));
            ulong required = unchecked((ulong)headerSize) + count * unchecked((ulong)entrySize);
            if (required > unchecked((ulong)bufferSize))
            {
                throw new InvalidOperationException("creator-host handle census entries were truncated");
            }

            ulong identityValue = unchecked((ulong)identityHandle.ToInt64());
            IntPtr processObject = IntPtr.Zero;
            int identityMatches = 0;
            for (ulong index = 0; index < count; index++)
            {
                int offset = checked(headerSize + (int)(index * unchecked((ulong)entrySize)));
                SystemHandleTableEntryInfoEx entry = (SystemHandleTableEntryInfoEx)Marshal.PtrToStructure(
                    IntPtr.Add(buffer, offset), typeof(SystemHandleTableEntryInfoEx));
                if (entry.UniqueProcessId.ToUInt64() == currentPid && entry.HandleValue.ToUInt64() == identityValue)
                {
                    identityMatches++;
                    processObject = entry.Object;
                }
            }
            if (identityMatches != 1 || processObject == IntPtr.Zero)
            {
                throw new InvalidOperationException("creator-host identity handle was not uniquely visible in system handle table");
            }

            int competing = 0;
            for (ulong index = 0; index < count; index++)
            {
                int offset = checked(headerSize + (int)(index * unchecked((ulong)entrySize)));
                SystemHandleTableEntryInfoEx entry = (SystemHandleTableEntryInfoEx)Marshal.PtrToStructure(
                    IntPtr.Add(buffer, offset), typeof(SystemHandleTableEntryInfoEx));
                if (entry.Object == processObject &&
                    entry.UniqueProcessId.ToUInt64() != currentPid &&
                    (entry.GrantedAccess & DANGEROUS_PROCESS_ACCESS) != 0)
                {
                    competing++;
                }
            }
            if (competing != 0)
            {
                throw new InvalidOperationException(
                    String.Format("creator-host security fence found {0} pre-existing external dangerous process handle(s)", competing));
            }
        }
    }
}
'@

if ($null -ne ('Autosport.Release.CreatorHostFence' -as [type])) {
  throw 'Creator-host fence type must not be preloaded before release bootstrap'
}
Add-Type -TypeDefinition $creatorFenceSource -Language CSharp

$creatorTestCoordination = [string]$env:AUTOSPORT_TEST_CREATOR_FENCE_COORDINATION
$creatorTestCanary = [IntPtr]::Zero
$creatorTestExpected = [long]0x102030405060708
if (-not [string]::IsNullOrWhiteSpace($creatorTestCoordination)) {
  $creatorTestCanary = [System.Runtime.InteropServices.Marshal]::AllocHGlobal(8)
  [System.Runtime.InteropServices.Marshal]::WriteInt64($creatorTestCanary, 0, [long]0x11223344556677)
  [System.IO.File]::WriteAllText(
    $creatorTestCoordination,
    "$PID`n$($creatorTestCanary.ToInt64())`n",
    [System.Text.UTF8Encoding]::new($false)
  )
  $creatorTestRelease = $creatorTestCoordination + '.release'
  $deadline = [DateTime]::UtcNow.AddSeconds(30)
  while (-not (Test-Path -LiteralPath $creatorTestRelease -PathType Leaf)) {
    if ([DateTime]::UtcNow -ge $deadline) {
      throw 'Creator-host pre-fence mutation probe timed out waiting for release signal'
    }
    Start-Sleep -Milliseconds 25
  }
  $observed = [System.Runtime.InteropServices.Marshal]::ReadInt64($creatorTestCanary, 0)
  if ($observed -ne $creatorTestExpected) {
    throw "Creator-host pre-fence mutation probe did not persist expected mutation: $observed"
  }
  Write-Output 'AUTOSPORT_CREATOR_PRE_FENCE_MUTATION_OBSERVED=PASS'
}

$creatorFence = $null
$creatorBodyTemp = $null
try {
  # Fence first. No release body, PyInstaller orchestrator assembly, or launch tuple
  # exists in this process before this acquisition and pre-existing-handle census.
  $creatorFence = [Autosport.Release.CreatorHostFence]::Acquire()

  $probePythonCommands = @(Get-Command python -CommandType Application -ErrorAction Stop)
  if ($probePythonCommands.Count -lt 1) { throw 'Unable to resolve Python application for creator-host fence probe' }
  $probePython = [string]$probePythonCommands[0].Source
  if ([string]::IsNullOrWhiteSpace($probePython)) { throw 'Resolved Python application for creator-host fence probe has an empty source path' }
  $creatorFreshAccessProbe = @'
import ctypes,sys
from ctypes import wintypes
ERROR_ACCESS_DENIED=5
pid=int(sys.argv[1])
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
k.OpenProcess.restype=wintypes.HANDLE
k.CloseHandle.argtypes=(wintypes.HANDLE,)
for access in (0x2,0x8,0x20,0x40,0x40000,0x80000):
    ctypes.set_last_error(0)
    h=k.OpenProcess(access,False,pid)
    v=h if isinstance(h,int) else ctypes.cast(h,ctypes.c_void_p).value
    if v:
        k.CloseHandle(h)
        raise SystemExit(f'CREATOR_DANGEROUS_ACCESS_AVAILABLE:{access}')
    error=ctypes.get_last_error()
    if error!=ERROR_ACCESS_DENIED:
        raise SystemExit(f'CREATOR_OPEN_PROCESS_FAILED:{access}:{error}')
print('DENIED',flush=True)
'@
  $creatorProbeOutput = @(& $probePython -I -c $creatorFreshAccessProbe $PID)
  if ($LASTEXITCODE -ne 0 -or $creatorProbeOutput.Count -ne 1 -or [string]$creatorProbeOutput[0] -ne 'DENIED') {
    throw "Creator-host fresh dangerous-access denial probe failed: $($creatorProbeOutput -join ' ')"
  }
  Write-Output 'AUTOSPORT_CREATOR_FENCE_FRESH_OPEN_DENIAL=PASS'

  if (-not [string]::IsNullOrWhiteSpace($creatorTestCoordination)) {
    Write-Output 'AUTOSPORT_CREATOR_RELEASE_BODY_LOADED=false'
    return
  }

  # Only now resolve/materialize the release body. It is a tracked alias of the
  # exact predecessor build-script blob, so this security repair does not rewrite
  # or fork the already-reviewed release implementation.
  Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' } | ForEach-Object {
    Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction SilentlyContinue
  }
  $env:GIT_NO_REPLACE_OBJECTS = '1'
  $gitCommands = @(Get-Command git -CommandType Application -ErrorAction Stop)
  if ($gitCommands.Count -lt 1) { throw 'Unable to resolve Git application for creator-host body bootstrap' }
  $gitExecutable = [System.IO.Path]::GetFullPath([string]$gitCommands[0].Source)
  if ([string]::IsNullOrWhiteSpace($gitExecutable)) { throw 'Resolved Git application for creator-host body bootstrap has an empty source path' }
  $checkoutHead = (& $gitExecutable rev-parse HEAD).Trim()
  if ($LASTEXITCODE -ne 0) { throw "Unable to resolve creator-host checkout HEAD; git exited $LASTEXITCODE" }
  $sourceSha = [string]$env:AUTOSPORT_SOURCE_SHA
  if ([string]::IsNullOrWhiteSpace($sourceSha)) { $sourceSha = $checkoutHead }
  if ($sourceSha -notmatch '^[0-9a-f]{40}$') { throw 'Creator-host source SHA is not canonical' }
  if ($sourceSha -ne $checkoutHead) { throw 'Creator-host exact source SHA does not match checkout HEAD' }

  $bodyEntry = (& $gitExecutable ls-tree $sourceSha -- 'scripts/build_windows_body.ps1').Trim()
  if ($LASTEXITCODE -ne 0) { throw "Unable to resolve exact creator-host build body; git exited $LASTEXITCODE" }
  $bodyMatch = [regex]::Match($bodyEntry, '^(100644|100755) blob ([0-9a-f]{40})\tscripts/build_windows_body\.ps1$')
  if (-not $bodyMatch.Success) { throw 'Exact source does not contain one regular creator-host build body blob' }
  $bodyBlobSha = $bodyMatch.Groups[2].Value
  if ($bodyBlobSha -ne '6116d30fd70aa5c0f7f115b03fdf27170d2d53c4') {
    throw "Creator-host build body blob is not the reviewed predecessor body: $bodyBlobSha"
  }

  $bodyInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $bodyInfo.FileName = $gitExecutable
  $bodyInfo.UseShellExecute = $false
  $bodyInfo.RedirectStandardOutput = $true
  $bodyInfo.RedirectStandardError = $true
  $bodyInfo.CreateNoWindow = $true
  [void]$bodyInfo.ArgumentList.Add('cat-file')
  [void]$bodyInfo.ArgumentList.Add('blob')
  [void]$bodyInfo.ArgumentList.Add($bodyBlobSha)
  $bodyProcess = [System.Diagnostics.Process]::new()
  $bodyProcess.StartInfo = $bodyInfo
  $bodyBuffer = [System.IO.MemoryStream]::new()
  try {
    if (-not $bodyProcess.Start()) { throw 'Unable to start exact creator-host body bootstrap' }
    $bodyProcess.StandardOutput.BaseStream.CopyTo($bodyBuffer)
    $bodyMessage = $bodyProcess.StandardError.ReadToEnd().Trim()
    $bodyProcess.WaitForExit()
    if ($bodyProcess.ExitCode -ne 0) {
      throw "Unable to materialize exact creator-host build body (git exit $($bodyProcess.ExitCode)): $bodyMessage"
    }
    $bodyBytes = $bodyBuffer.ToArray()
  } finally {
    $bodyBuffer.Dispose()
    $bodyProcess.Dispose()
  }
  if ($bodyBytes.Length -eq 0) { throw 'Exact creator-host build body blob is empty' }
  $creatorBodyTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("autosport-build-body-" + [guid]::NewGuid().ToString('N') + '.ps1')
  [System.IO.File]::WriteAllBytes($creatorBodyTemp, $bodyBytes)
  & $creatorBodyTemp
} finally {
  if (-not [string]::IsNullOrWhiteSpace([string]$creatorBodyTemp)) {
    Remove-Item -LiteralPath $creatorBodyTemp -Force -ErrorAction SilentlyContinue
  }
  if ($null -ne $creatorFence) {
    $creatorFence.Dispose()
  }
  if ($creatorTestCanary -ne [IntPtr]::Zero) {
    [System.Runtime.InteropServices.Marshal]::FreeHGlobal($creatorTestCanary)
  }
}
