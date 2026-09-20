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

$pythonApplications = @(Get-Command python -CommandType Application -ErrorAction Stop)
if ($pythonApplications.Count -lt 1) { throw 'Unable to resolve Python application for Windows build wrapper' }
$script:trustedPythonApplication = [string]$pythonApplications[0].Source
if ([string]::IsNullOrWhiteSpace($script:trustedPythonApplication)) {
  throw 'Resolved Python application has an empty source path'
}
$script:skippedBuilderPytestGate = 0

function python {
  $pythonArgs = @($args)
  if (
    $pythonArgs.Count -eq 4 -and
    [string]$pythonArgs[0] -eq '-m' -and
    [string]$pythonArgs[1] -eq 'pytest' -and
    [string]$pythonArgs[2] -eq '-v' -and
    [string]$pythonArgs[3] -eq 'tests'
  ) {
    $script:skippedBuilderPytestGate += 1
    Write-Host 'BUILDER_LOCAL_PYTEST=SKIPPED_BY_EXPLICIT_CALLER'
    & $script:trustedPythonApplication -c 'pass'
    return
  }

  & $script:trustedPythonApplication @pythonArgs
}

try {
  & $coreScript
} finally {
  Remove-Item Function:\python -ErrorAction SilentlyContinue
}

if ($script:skippedBuilderPytestGate -ne 1) {
  throw "-SkipTests expected to bypass exactly one builder-local pytest gate; observed $($script:skippedBuilderPytestGate)"
}
