$ErrorActionPreference = 'Stop'

# The GitHub-hosted Windows runner may assign SeDebugPrivilege to the PowerShell
# token even when it is disabled. A process DACL cannot be an authoritative
# dangerous-access fence while that privilege remains assigned, because it can be
# enabled later to bypass ordinary process access checks. Remove it irreversibly
# before loading the existing creator-host fence or any release implementation.
$creatorPrivilegeBootstrapSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Autosport.Release
{
    public static class CreatorHostPrivilegeBootstrap
    {
        private const uint TOKEN_ADJUST_PRIVILEGES = 0x0020;
        private const uint TOKEN_QUERY = 0x0008;
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

        public static void RemoveSeDebugPrivilegeAndVerifyAbsent()
        {
            IntPtr token = IntPtr.Zero;
            if (!OpenProcessToken(
                    GetCurrentProcess(),
                    TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                    out token))
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "creator-host token cannot be opened for SeDebugPrivilege removal");
            }

            try
            {
                Luid luid;
                if (!LookupPrivilegeValueW(null, "SeDebugPrivilege", out luid))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "creator-host SeDebugPrivilege lookup failed before removal");
                }

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

                // An AdjustTokenPrivileges assignment attempt is also the fail-closed
                // verification oracle: after SE_PRIVILEGE_REMOVED, Windows must report
                // ERROR_NOT_ALL_ASSIGNED. Success would prove the privilege is still
                // present in the token and the later process-DACL proof is unsafe.
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
            }
            finally
            {
                if (token != IntPtr.Zero)
                {
                    CloseHandle(token);
                }
            }
        }
    }
}
'@

if ($null -ne ('Autosport.Release.CreatorHostPrivilegeBootstrap' -as [type])) {
  throw 'Creator-host privilege bootstrap type must not be preloaded'
}
Add-Type -TypeDefinition $creatorPrivilegeBootstrapSource -Language CSharp
[Autosport.Release.CreatorHostPrivilegeBootstrap]::RemoveSeDebugPrivilegeAndVerifyAbsent()
Write-Output 'AUTOSPORT_CREATOR_SEDEBUG_REMOVAL=PASS'

# Keep the existing creator-host fence byte-for-byte unchanged. Open it once with
# FileShare.Read (writers/deleters denied), bind the exact tracked Git blob identity,
# parse the captured bytes in memory, and keep the source handle open through its
# execution. The release body remains behind that existing fence.
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
  $creatorFenceScriptBlock = [ScriptBlock]::Create($creatorFenceText)
  & $creatorFenceScriptBlock
} finally {
  $creatorFenceStream.Dispose()
}
