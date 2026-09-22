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

# The canonical candidate path also materializes the stage-neutral one-product
# release artifact. The legacy package remains only as the already-qualified input
# consumed by the transition authority and by older downstream checks.
$materializer = Join-Path $PSScriptRoot 'materialize_stage_neutral_release.ps1'
if (-not (Test-Path -LiteralPath $materializer -PathType Leaf)) {
  throw 'Stage-neutral Windows release materializer is missing'
}

$sourceSha = [string]$env:AUTOSPORT_SOURCE_SHA
if ([string]::IsNullOrWhiteSpace($sourceSha)) {
  $repoRoot = Split-Path -Parent $PSScriptRoot
  $sourceSha = (& git -C $repoRoot rev-parse HEAD 2>&1 | Out-String).Trim()
  if ($LASTEXITCODE -ne 0) {
    throw 'Unable to resolve exact source SHA for stage-neutral release materialization'
  }
}
if ($sourceSha -notmatch '^[0-9a-f]{40}$') {
  throw 'Stage-neutral release materialization requires a canonical lowercase Git SHA'
}

& $materializer -SourceSha $sourceSha
if ($LASTEXITCODE -ne 0) {
  throw "Stage-neutral Windows release materialization exited with code $LASTEXITCODE"
}
