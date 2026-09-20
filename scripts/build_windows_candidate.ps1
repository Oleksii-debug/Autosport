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
