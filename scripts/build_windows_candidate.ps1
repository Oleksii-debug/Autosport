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
try {
  Invoke-WindowsCandidateCoreText -CoreText $candidateText
} catch {
  # The canonical builder writes restart/recovery failure evidence before it returns a
  # non-zero exit. Emit that bounded machine evidence before propagating the original
  # failure so Actions can diagnose a packaged recovery gate without weakening it.
  $restartRecoveryAudit = Join-Path (Get-Location).Path 'dist/restart-recovery-audit.json'
  if (Test-Path -LiteralPath $restartRecoveryAudit -PathType Leaf) {
    Write-Host '--- packaged restart/recovery audit evidence ---'
    Write-Host ([System.IO.File]::ReadAllText($restartRecoveryAudit))
    Write-Host '--- end packaged restart/recovery audit evidence ---'
  }
  throw
}
