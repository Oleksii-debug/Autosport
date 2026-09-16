if (-not ("Autosport.Release.PackagedExecutablePathFence" -as [type])) {
  Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using Microsoft.Win32.SafeHandles;

namespace Autosport.Release
{
    public sealed class QualifiedExecutableAuthority : IDisposable
    {
        private FileStream stream;
        private List<SafeFileHandle> componentHandles;

        internal QualifiedExecutableAuthority(
            string path,
            FileStream stream,
            List<SafeFileHandle> componentHandles)
        {
            Path = path;
            this.stream = stream;
            this.componentHandles = componentHandles;
        }

        public string Path { get; private set; }
        public FileStream Stream { get { return stream; } }

        public void Dispose()
        {
            if (stream != null)
            {
                stream.Dispose();
                stream = null;
            }
            if (componentHandles != null)
            {
                for (int index = componentHandles.Count - 1; index >= 0; index--)
                {
                    componentHandles[index].Dispose();
                }
                componentHandles.Clear();
                componentHandles = null;
            }
        }
    }

    public static class PackagedExecutablePathFence
    {
        private const uint FILE_READ_ATTRIBUTES = 0x00000080;
        private const uint FILE_SHARE_READ = 0x00000001;
        private const uint OPEN_EXISTING = 3;
        private const uint FILE_ATTRIBUTE_DIRECTORY = 0x00000010;
        private const uint FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400;
        private const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
        private const uint FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;

        [StructLayout(LayoutKind.Sequential)]
        private struct BY_HANDLE_FILE_INFORMATION
        {
            public uint dwFileAttributes;
            public FILETIME ftCreationTime;
            public FILETIME ftLastAccessTime;
            public FILETIME ftLastWriteTime;
            public uint dwVolumeSerialNumber;
            public uint nFileSizeHigh;
            public uint nFileSizeLow;
            public uint nNumberOfLinks;
            public uint nFileIndexHigh;
            public uint nFileIndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(
            string lpFileName,
            uint dwDesiredAccess,
            uint dwShareMode,
            IntPtr lpSecurityAttributes,
            uint dwCreationDisposition,
            uint dwFlagsAndAttributes,
            IntPtr hTemplateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle hFile,
            out BY_HANDLE_FILE_INFORMATION lpFileInformation);

        private static SafeFileHandle OpenComponent(string path, bool leaf)
        {
            SafeFileHandle handle = CreateFileW(
                path,
                FILE_READ_ATTRIBUTES,
                FILE_SHARE_READ,
                IntPtr.Zero,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                IntPtr.Zero);
            if (handle.IsInvalid)
            {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "qualified executable path component could not be pinned: " + path);
            }

            BY_HANDLE_FILE_INFORMATION info;
            if (!GetFileInformationByHandle(handle, out info))
            {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "qualified executable path component identity could not be read: " + path);
            }
            if ((info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0)
            {
                handle.Dispose();
                throw new InvalidOperationException(
                    "qualified executable path component is a reparse point: " + path);
            }
            bool isDirectory = (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
            if (leaf ? isDirectory : !isDirectory)
            {
                handle.Dispose();
                throw new InvalidOperationException(
                    leaf
                        ? "qualified executable leaf is not a regular file: " + path
                        : "qualified executable ancestor is not a directory: " + path);
            }
            return handle;
        }

        public static QualifiedExecutableAuthority Acquire(string path)
        {
            if (String.IsNullOrWhiteSpace(path))
            {
                throw new ArgumentException("qualified executable path is empty", "path");
            }
            string fullPath = Path.GetFullPath(path);
            if (!Path.IsPathRooted(fullPath))
            {
                throw new InvalidOperationException("qualified executable path must be absolute");
            }
            string root = Path.GetPathRoot(fullPath);
            if (String.IsNullOrEmpty(root) || fullPath.Length <= root.Length)
            {
                throw new InvalidOperationException("qualified executable path has no leaf component");
            }
            string relative = fullPath.Substring(root.Length);
            if (relative.IndexOf(':') >= 0)
            {
                throw new InvalidOperationException("qualified executable path must not use an alternate data stream");
            }

            string[] parts = relative.Split(
                new char[] { Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar },
                StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 0)
            {
                throw new InvalidOperationException("qualified executable path has no components");
            }

            List<string> componentPaths = new List<string>();
            componentPaths.Add(root);
            string current = root;
            foreach (string part in parts)
            {
                current = Path.Combine(current, part);
                componentPaths.Add(current);
            }

            List<SafeFileHandle> handles = new List<SafeFileHandle>();
            FileStream stream = null;
            try
            {
                for (int index = 0; index < componentPaths.Count; index++)
                {
                    handles.Add(OpenComponent(componentPaths[index], index == componentPaths.Count - 1));
                }
                stream = new FileStream(fullPath, FileMode.Open, FileAccess.Read, FileShare.Read);
                return new QualifiedExecutableAuthority(fullPath, stream, handles);
            }
            catch
            {
                if (stream != null)
                {
                    stream.Dispose();
                }
                for (int index = handles.Count - 1; index >= 0; index--)
                {
                    handles[index].Dispose();
                }
                throw;
            }
        }
    }
}
'@
}


function Get-AutosportProducerPackageIdentity {
  [CmdletBinding()]
  param(
    [string]$PackagePath = (Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'),
    [string]$ExpectedPackageSha256 = [string]$env:AUTOSPORT_PRODUCER_PACKAGE_SHA256,
    [string]$ExpectedSourceSha = [string]$env:AUTOSPORT_SOURCE_SHA
  )

  if (-not (Test-Path $PackagePath -PathType Leaf)) {
    throw "Producer-bound package identity source is missing: $PackagePath"
  }
  $ExpectedPackageSha256 = $ExpectedPackageSha256.Trim().ToLowerInvariant()
  $ExpectedSourceSha = $ExpectedSourceSha.Trim().ToLowerInvariant()
  if ($ExpectedPackageSha256 -notmatch '^[0-9a-f]{64}$') {
    throw 'Producer-bound package identity requires a canonical expected package SHA-256'
  }
  if ($ExpectedSourceSha -notmatch '^[0-9a-f]{40}$') {
    throw 'Producer-bound package identity requires a canonical exact source SHA'
  }

  $stream = [System.IO.File]::Open(
    $PackagePath,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::Read
  )
  $archive = $null
  try {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
      $actualPackageSha = [System.Convert]::ToHexString(
        $sha256.ComputeHash($stream)
      ).ToLowerInvariant()
    } finally {
      $sha256.Dispose()
    }
    if ($actualPackageSha -ne $ExpectedPackageSha256) {
      throw "Producer-bound package SHA-256 mismatch: expected $ExpectedPackageSha256, got $actualPackageSha"
    }

    $stream.Position = 0
    $archive = [System.IO.Compression.ZipArchive]::new(
      $stream,
      [System.IO.Compression.ZipArchiveMode]::Read,
      $true
    )
    $buildInfoEntry = $archive.GetEntry('Autosport-V1/BUILD_INFO.json')
    if ($null -eq $buildInfoEntry) {
      throw 'Producer-bound package is missing Autosport-V1/BUILD_INFO.json'
    }
    $entryStream = $buildInfoEntry.Open()
    try {
      $reader = [System.IO.StreamReader]::new(
        $entryStream,
        [System.Text.Encoding]::UTF8,
        $true,
        4096,
        $true
      )
      try {
        $buildInfoJson = $reader.ReadToEnd()
      } finally {
        $reader.Dispose()
      }
    } finally {
      $entryStream.Dispose()
    }
    $buildInfo = $buildInfoJson | ConvertFrom-Json

    $sourceSha = ([string]$buildInfo.source_sha).Trim().ToLowerInvariant()
    $autosportExeSha = ([string]$buildInfo.autosport_exe_sha256).Trim().ToLowerInvariant()
    $autosportDataExeSha = ([string]$buildInfo.autosport_data_exe_sha256).Trim().ToLowerInvariant()
    if ($sourceSha -ne $ExpectedSourceSha) {
      throw "Producer-bound BUILD_INFO source_sha mismatch: expected $ExpectedSourceSha, got $sourceSha"
    }
    if ($autosportExeSha -notmatch '^[0-9a-f]{64}$' -or $autosportDataExeSha -notmatch '^[0-9a-f]{64}$') {
      throw 'Producer-bound BUILD_INFO executable SHA-256 values are not canonical'
    }
    if ($buildInfo.real_money_execution -ne $false -or
        $buildInfo.human_tested -ne $false -or
        $buildInfo.nvda_verified -ne $false) {
      throw 'Producer-bound BUILD_INFO violated prehuman truth labels'
    }

    return [pscustomobject]@{
      PackageSha256 = $actualPackageSha
      SourceSha = $sourceSha
      AutosportExeSha256 = $autosportExeSha
      AutosportDataExeSha256 = $autosportDataExeSha
    }
  } finally {
    if ($null -ne $archive) { $archive.Dispose() }
    $stream.Dispose()
  }
}


function Open-AutosportQualifiedExecutable {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$ExpectedSha256,
    [Parameter(Mandatory = $true)][string]$Label
  )

  if ([string]::IsNullOrWhiteSpace($Path)) {
    throw "$Label is missing: $Path"
  }
  if (-not [System.IO.Path]::IsPathFullyQualified($Path)) {
    throw "$Label path must be absolute: $Path"
  }
  $fullPath = [System.IO.Path]::GetFullPath($Path)
  if (-not [string]::Equals($fullPath, $Path, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "$Label path must be canonical before qualification: $Path"
  }
  $ExpectedSha256 = $ExpectedSha256.Trim().ToLowerInvariant()
  if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') {
    throw "$Label expected executable SHA-256 is not canonical"
  }

  # Acquire a non-reparse handle for every lexical path component before hashing.
  # Each native handle shares reads only, so a pre-opened writer/delete-capable
  # handle makes acquisition fail closed and later rename/reparse substitution stays
  # blocked until the caller disposes the authority after process completion.
  $authority = [Autosport.Release.PackagedExecutablePathFence]::Acquire($fullPath)
  try {
    $stream = $authority.Stream
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
      $actual = [System.Convert]::ToHexString(
        $sha256.ComputeHash($stream)
      ).ToLowerInvariant()
    } finally {
      $sha256.Dispose()
    }
    if ($actual -ne $ExpectedSha256) {
      throw "$Label SHA-256 mismatch before process start: expected $ExpectedSha256, got $actual"
    }
    $stream.Position = 0
    return $authority
  } catch {
    $authority.Dispose()
    throw
  }
}
