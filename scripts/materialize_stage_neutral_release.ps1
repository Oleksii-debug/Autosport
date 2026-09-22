param(
    [string]$SourceSha = $env:AUTOSPORT_SOURCE_SHA
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($SourceSha)) {
    throw 'Stage-neutral release materialization requires an exact source SHA via -SourceSha or AUTOSPORT_SOURCE_SHA.'
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $repoRoot 'dist'
$legacyPackage = Join-Path $distRoot 'Autosport-V1-windows-x64.zip'
$stageNeutralPackage = Join-Path $distRoot 'Autosport-windows-x64.zip'
$evidencePath = Join-Path $distRoot 'stage-neutral-release-verification.json'

if (-not (Test-Path -LiteralPath $legacyPackage -PathType Leaf)) {
    throw "Canonical legacy release package is missing: $legacyPackage"
}

New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

& python -m autosport.stage_neutral_release `
    --input $legacyPackage `
    --output $stageNeutralPackage `
    --source-sha $SourceSha `
    --evidence $evidencePath
if ($LASTEXITCODE -ne 0) {
    throw "Stage-neutral release repack exited with code $LASTEXITCODE"
}

foreach ($required in @($stageNeutralPackage, $evidencePath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Stage-neutral release materialization is missing required output: $required"
    }
}

$evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
if ($evidence.status -ne 'PASS') {
    throw 'Stage-neutral release evidence did not record PASS.'
}
if ([string]$evidence.source_sha -ne [string]$SourceSha) {
    throw 'Stage-neutral release evidence source_sha does not match requested source SHA.'
}
if ([string]$evidence.archive_name -ne 'Autosport-windows-x64.zip') {
    throw 'Stage-neutral release evidence archive identity drifted.'
}
if ([string]$evidence.package_prefix -ne 'Autosport/') {
    throw 'Stage-neutral release evidence package prefix drifted.'
}
if ([string]$evidence.build_version -ne '0.1.0-prehuman') {
    throw 'Stage-neutral release evidence build version drifted.'
}
if (
    $evidence.real_money_execution -ne $false -or
    $evidence.human_tested -ne $false -or
    $evidence.nvda_verified -ne $false -or
    $evidence.whole_product_complete -ne $false
) {
    throw 'Stage-neutral release evidence violated required false product-truth labels.'
}

$packageSha = (Get-FileHash -LiteralPath $stageNeutralPackage -Algorithm SHA256).Hash.ToLowerInvariant()
if ([string]$evidence.package_sha256 -ne $packageSha) {
    throw 'Stage-neutral release evidence package digest does not match authored ZIP.'
}

Write-Output "stage-neutral-release=PASS source_sha=$SourceSha package_sha256=$packageSha"
