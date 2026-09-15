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

  if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path $Path -PathType Leaf)) {
    throw "$Label is missing: $Path"
  }
  $ExpectedSha256 = $ExpectedSha256.Trim().ToLowerInvariant()
  if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') {
    throw "$Label expected executable SHA-256 is not canonical"
  }

  # FileShare.Read intentionally permits the Windows image loader to read the
  # executable while denying writers and delete/rename replacement for the full
  # hash -> CreateProcess -> consumer interval. Callers must Dispose() the returned
  # stream only after every launch in the consumer step has completed.
  $stream = [System.IO.File]::Open(
    $Path,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::Read
  )
  try {
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
    return $stream
  } catch {
    $stream.Dispose()
    throw
  }
}
