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
# script.  The opt-in candidate path removes only its one duplicate full-suite
# gate from the in-memory script text.  This is deliberately fail-closed: any
# future builder edit that changes, removes, or duplicates the exact gate makes
# -SkipTests fail before any build work rather than silently weakening coverage.
$coreText = [System.IO.File]::ReadAllText($coreScript)
$pytestGatePattern = '(?m)^python -m pytest -v tests\r?\nif \(\$LASTEXITCODE -ne 0\) \{ throw "Full pytest gate exited \$LASTEXITCODE" \}\r?\n'
$pytestGateRegex = [regex]::new($pytestGatePattern)
$pytestGateMatches = $pytestGateRegex.Matches($coreText)
if ($pytestGateMatches.Count -ne 1) {
  throw "-SkipTests requires exactly one canonical builder-local pytest gate; observed $($pytestGateMatches.Count)"
}

$replacement = "Write-Host 'BUILDER_LOCAL_PYTEST=SKIPPED_BY_EXPLICIT_CALLER'`n"
$candidateCoreText = $pytestGateRegex.Replace($coreText, $replacement, 1)
if ($candidateCoreText -eq $coreText -or $candidateCoreText.Contains('python -m pytest -v tests')) {
  throw '-SkipTests failed to remove exactly the canonical builder-local pytest invocation'
}

$candidateCore = [scriptblock]::Create($candidateCoreText)
& $candidateCore
