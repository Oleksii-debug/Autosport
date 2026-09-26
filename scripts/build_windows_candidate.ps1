$ErrorActionPreference = 'Stop'

# Windows Candidate-only accelerator. The canonical manual/release builder remains
# scripts/build_windows.ps1 so its established source/security contract is not
# relocated or weakened. This wrapper removes exactly the one redundant full-suite
# pytest gate only after the separate exact-head CI workflow has run that suite.
$builderScript = Join-Path $PSScriptRoot 'build_windows.ps1'
if (-not (Test-Path -LiteralPath $builderScript -PathType Leaf)) {
  throw 'Canonical Windows build script is missing'
}

$skipGateHelper = Join-Path $PSScriptRoot 'windows_build_skip_gate.ps1'
if (-not (Test-Path -LiteralPath $skipGateHelper -PathType Leaf)) {
  throw 'Windows candidate skip-gate helper is missing'
}
. $skipGateHelper

$builderText = [System.IO.File]::ReadAllText($builderScript)
$candidateText = ConvertTo-WindowsCandidateCoreText -CoreText $builderText
Invoke-WindowsCandidateCoreText -CoreText $candidateText

# The stage-neutral transition runs only from source_sha Git objects. Do not execute
# the live materializer path after the canonical build has finished: late checkout
# drift must not be able to redefine the release transformation authority.
$repoRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$gitCommands = @(Get-Command git -CommandType Application -ErrorAction Stop)
if ($gitCommands.Count -lt 1) {
  throw 'Unable to resolve Git application for stage-neutral release materialization'
}
$gitExecutable = [string]$gitCommands[0].Source
if ([string]::IsNullOrWhiteSpace($gitExecutable)) {
  throw 'Resolved Git application has an empty source path'
}

Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' } | ForEach-Object {
  Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction SilentlyContinue
}
$env:GIT_NO_REPLACE_OBJECTS = '1'

$checkoutHead = (& $gitExecutable -C $repoRoot rev-parse --verify HEAD 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
  throw 'Unable to resolve exact checkout HEAD for stage-neutral release materialization'
}
$sourceSha = [string]$env:AUTOSPORT_SOURCE_SHA
if ([string]::IsNullOrWhiteSpace($sourceSha)) {
  $sourceSha = $checkoutHead
}
if ($sourceSha -notmatch '^[0-9a-f]{40}$') {
  throw 'Stage-neutral release materialization requires a canonical lowercase Git SHA'
}
if ($sourceSha -ne $checkoutHead) {
  throw 'Stage-neutral release source SHA does not match checkout HEAD'
}

$gitTopLevel = (& $gitExecutable -C $repoRoot rev-parse --show-toplevel 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
  throw 'Unable to resolve Git top-level for stage-neutral release materialization'
}
if (
  -not [string]::Equals(
    [System.IO.Path]::GetFullPath($gitTopLevel),
    $repoRoot,
    [System.StringComparison]::OrdinalIgnoreCase
  )
) {
  throw 'Stage-neutral release Git top-level does not match repository root'
}
$replacementRefs = @(
  & $gitExecutable -C $repoRoot for-each-ref '--format=%(refname)' refs/replace |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
if ($LASTEXITCODE -ne 0) {
  throw 'Unable to enumerate Git replacement refs for stage-neutral release materialization'
}
if ($replacementRefs.Count -ne 0) {
  throw 'Stage-neutral release repository contains Git replacement refs'
}

$materializerRepoPath = 'scripts/materialize_stage_neutral_release.ps1'
$materializerTreeEntry = (
  & $gitExecutable -C $repoRoot ls-tree $sourceSha -- $materializerRepoPath 2>&1 |
    Out-String
).Trim()
if ($LASTEXITCODE -ne 0) {
  throw 'Unable to resolve exact stage-neutral materializer Git object'
}
$materializerMatch = [regex]::Match(
  $materializerTreeEntry,
  '^(100644|100755) blob ([0-9a-f]{40})\tscripts/materialize_stage_neutral_release\.ps1$'
)
if (-not $materializerMatch.Success) {
  throw 'Exact source does not contain one regular stage-neutral materializer blob'
}
$materializerBlobSha = $materializerMatch.Groups[2].Value

$materializerInfo = [System.Diagnostics.ProcessStartInfo]::new()
$materializerInfo.FileName = $gitExecutable
$materializerInfo.UseShellExecute = $false
$materializerInfo.RedirectStandardOutput = $true
$materializerInfo.RedirectStandardError = $true
$materializerInfo.CreateNoWindow = $true
[void]$materializerInfo.ArgumentList.Add('-C')
[void]$materializerInfo.ArgumentList.Add($repoRoot)
[void]$materializerInfo.ArgumentList.Add('cat-file')
[void]$materializerInfo.ArgumentList.Add('blob')
[void]$materializerInfo.ArgumentList.Add($materializerBlobSha)
$materializerProcess = [System.Diagnostics.Process]::new()
$materializerProcess.StartInfo = $materializerInfo
$materializerBuffer = [System.IO.MemoryStream]::new()
try {
  if (-not $materializerProcess.Start()) {
    throw 'Unable to start exact stage-neutral materializer bootstrap'
  }
  $materializerProcess.StandardOutput.BaseStream.CopyTo($materializerBuffer)
  $materializerError = $materializerProcess.StandardError.ReadToEnd().Trim()
  $materializerProcess.WaitForExit()
  if ($materializerProcess.ExitCode -ne 0) {
    throw "Unable to materialize exact stage-neutral materializer blob (git exit $($materializerProcess.ExitCode)): $materializerError"
  }
  $materializerBytes = $materializerBuffer.ToArray()
} finally {
  $materializerBuffer.Dispose()
  $materializerProcess.Dispose()
}
if ($materializerBytes.Length -eq 0) {
  throw 'Exact stage-neutral materializer blob is empty'
}

$strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
try {
  $materializerText = $strictUtf8.GetString($materializerBytes)
} catch {
  throw 'Exact stage-neutral materializer blob is not valid UTF-8'
}
$materializerScript = [scriptblock]::Create($materializerText)
& $materializerScript -SourceSha $sourceSha -RepoRoot $repoRoot
