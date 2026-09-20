param(
  [switch] $SkipTests
)

$ErrorActionPreference = 'Stop'
$coreScript = Join-Path $PSScriptRoot 'build_windows_core.ps1'
if (-not (Test-Path -LiteralPath $coreScript -PathType Leaf)) {
  throw 'Windows build core script is missing'
}

if (-not $SkipTests) {
  & $coreScript
  return
}

# The production builder remains byte-identical to the proven pre-optimization
# script. The opt-in candidate path removes only its one duplicate full-suite
# gate from in-memory script text. The transform is exact and fail-closed: any
# future builder edit that changes, removes, or duplicates that gate aborts the
# candidate before build work instead of silently weakening coverage.
$skipGateHelper = Join-Path $PSScriptRoot 'windows_build_skip_gate.ps1'
if (-not (Test-Path -LiteralPath $skipGateHelper -PathType Leaf)) {
  throw 'Windows candidate skip-gate helper is missing'
}
. $skipGateHelper

$coreText = [System.IO.File]::ReadAllText($coreScript)
$candidateCoreText = ConvertTo-WindowsCandidateCoreText -CoreText $coreText
$candidateCore = [scriptblock]::Create($candidateCoreText)
& $candidateCore
