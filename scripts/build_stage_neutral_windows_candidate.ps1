param(
    [string]$SourceSha = $env:AUTOSPORT_SOURCE_SHA
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($SourceSha)) {
    $SourceSha = (& git -C $repoRoot rev-parse HEAD 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to resolve exact source SHA for stage-neutral Windows candidate build.'
    }
}
if ($SourceSha -notmatch '^[0-9a-f]{40}$') {
    throw 'Stage-neutral Windows candidate build requires a canonical 40-character lowercase Git SHA.'
}

$env:AUTOSPORT_SOURCE_SHA = $SourceSha

$candidateBuilder = Join-Path $PSScriptRoot 'build_windows_candidate.ps1'
$materializer = Join-Path $PSScriptRoot 'materialize_stage_neutral_release.ps1'
foreach ($required in @($candidateBuilder, $materializer)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Stage-neutral Windows candidate build is missing required script: $required"
    }
}

& $candidateBuilder
if ($LASTEXITCODE -ne 0) {
    throw "Canonical Windows candidate build exited with code $LASTEXITCODE"
}

& $materializer -SourceSha $SourceSha
if ($LASTEXITCODE -ne 0) {
    throw "Stage-neutral Windows release materialization exited with code $LASTEXITCODE"
}

$stageNeutralPackage = Join-Path $repoRoot 'dist/Autosport-windows-x64.zip'
$stageNeutralEvidence = Join-Path $repoRoot 'dist/stage-neutral-release-verification.json'
foreach ($required in @($stageNeutralPackage, $stageNeutralEvidence)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Stage-neutral Windows candidate build is missing required output: $required"
    }
}

Write-Output "stage-neutral-windows-candidate=PASS source_sha=$SourceSha"
