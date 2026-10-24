param(
    [Parameter(Mandatory = $true)][string]$CurrentPackage,
    [Parameter(Mandatory = $true)][string]$PackagedDataExe,
    [Parameter(Mandatory = $true)][string]$SourceSha,
    [Parameter(Mandatory = $true)][string]$CurrentPackageSha256,
    [Parameter(Mandatory = $true)][string]$EvidencePath
)

$ErrorActionPreference = 'Stop'
if ($SourceSha -cnotmatch '^[0-9a-f]{40}$') { throw 'Update lifecycle requires canonical source SHA' }
if ($CurrentPackageSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Update lifecycle requires canonical package SHA-256' }
foreach ($path in @($CurrentPackage, $PackagedDataExe)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Update lifecycle input is missing' }
}

function Invoke-ReleaseAction {
    param([string[]]$Arguments, [string]$ExpectedStatus)
    $raw = (& $PackagedDataExe @Arguments 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Packaged update lifecycle action failed closed unexpectedly' }
    $record = $raw | ConvertFrom-Json -ErrorAction Stop
    if ([string]$record.status -cne $ExpectedStatus -or $record.real_money_execution -ne $false) {
        throw 'Packaged update lifecycle action emitted unexpected evidence'
    }
    return $record
}

$root = Join-Path $PWD 'dist/packaged-update-lifecycle-smoke'
if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
New-Item -ItemType Directory -Path $root | Out-Null
$previousPackage = Join-Path $root 'Autosport-previous-fixture.zip'
$fixtureText = (& python scripts/derive_previous_release_fixture.py `
    --current-archive $CurrentPackage `
    --output $previousPackage `
    --source-sha $SourceSha `
    --package-sha256 $CurrentPackageSha256 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Previous-release fixture derivation failed' }
$fixture = $fixtureText | ConvertFrom-Json -ErrorAction Stop
if (
    $fixture.status -cne 'PASS' -or
    $fixture.evidence_class -cne 'FIXTURE_ONLY_PREVIOUS_VERSION_ENVELOPE' -or
    $fixture.source_sha -cne $SourceSha -or
    $fixture.update_version -cne '0.0.0' -or
    [int]$fixture.update_schema -ne 1 -or
    $fixture.historical_release -ne $false
) { throw 'Previous-release fixture evidence is invalid' }
$previousSha = [string]$fixture.package_sha256
if ($previousSha -cnotmatch '^[0-9a-f]{64}$' -or $previousSha -ceq $CurrentPackageSha256) {
    throw 'Previous-release fixture digest is invalid'
}

$oldCommon = @('--archive', $previousPackage, '--source-sha', $SourceSha, '--package-sha256', $previousSha)
$newCommon = @('--new-archive', $CurrentPackage, '--new-source-sha', $SourceSha, '--new-package-sha256', $CurrentPackageSha256)
$oldUpdate = @('--old-archive', $previousPackage, '--old-source-sha', $SourceSha, '--old-package-sha256', $previousSha)

# Real packaged upgrade -> verify -> rollback -> verify -> finalize.
$destination = Join-Path $root 'Оновлення з пробілами'
[void](Invoke-ReleaseAction -Arguments (@('install-release', '--destination', $destination) + $oldCommon) -ExpectedStatus 'INSTALLED')
$updated = Invoke-ReleaseAction -Arguments (@('upgrade-release', '--destination', $destination) + $oldUpdate + $newCommon) -ExpectedStatus 'UPDATED_PAPER_ONLY'
if ($updated.schema_migrated -ne $false -or $updated.package_sha256 -cne $CurrentPackageSha256) {
    throw 'Packaged same-schema update evidence drifted'
}
[void](Invoke-ReleaseAction -Arguments @('verify-installed-release', '--destination', $destination, '--archive', $CurrentPackage, '--source-sha', $SourceSha, '--package-sha256', $CurrentPackageSha256) -ExpectedStatus 'VERIFIED')
[void](Invoke-ReleaseAction -Arguments (@('rollback-release', '--destination', $destination) + $oldUpdate + $newCommon) -ExpectedStatus 'ROLLED_BACK_PAPER_ONLY')
[void](Invoke-ReleaseAction -Arguments (@('verify-installed-release', '--destination', $destination) + $oldCommon) -ExpectedStatus 'VERIFIED')
[void](Invoke-ReleaseAction -Arguments (@('finalize-update-release', '--destination', $destination) + $oldUpdate + $newCommon) -ExpectedStatus 'PAPER_UPDATE_FINALIZED')

# Recreate the exact on-disk interruption window (old product moved, root gap),
# then require the packaged recovery command to restore and reverify it.
$recoveryDestination = Join-Path $root 'Перерване оновлення'
[void](Invoke-ReleaseAction -Arguments (@('install-release', '--destination', $recoveryDestination) + $oldCommon) -ExpectedStatus 'INSTALLED')
$setup = @'
from pathlib import Path
import sys
from autosport.offline_release_upgrade import _transaction_root
destination = Path(sys.argv[1])
transaction = _transaction_root(destination)
transaction.mkdir(mode=0o700)
destination.rename(transaction / "previous")
'@
& python -c $setup $recoveryDestination
if ($LASTEXITCODE -ne 0 -or (Test-Path -LiteralPath $recoveryDestination)) {
    throw 'Interrupted update fixture was not created'
}
[void](Invoke-ReleaseAction -Arguments (@('recover-update-release', '--destination', $recoveryDestination) + $oldUpdate) -ExpectedStatus 'OLD_RELEASE_RECOVERED')
[void](Invoke-ReleaseAction -Arguments (@('verify-installed-release', '--destination', $recoveryDestination) + $oldCommon) -ExpectedStatus 'VERIFIED')

foreach ($record in @($fixture, $updated)) {
    foreach ($field in @('real_money_execution', 'human_tested', 'nvda_verified')) {
        if ($null -ne $record.$field -and $record.$field -ne $false) {
            throw "Update lifecycle evidence $field is not false"
        }
    }
}

[ordered]@{
    status = 'PASS'
    evidence_class = 'EXACT_WINDOWS_PACKAGED_UPDATE_ROLLBACK_RECOVERY'
    source_sha = $SourceSha
    current_package_sha256 = $CurrentPackageSha256
    previous_fixture_package_sha256 = $previousSha
    previous_release_scope = 'fixture_only_same_candidate_bytes_with_prior_version_envelope'
    historical_release_qualified = $false
    update_status = 'UPDATED_PAPER_ONLY'
    rollback_status = 'ROLLED_BACK_PAPER_ONLY'
    interrupted_update_recovery_status = 'OLD_RELEASE_RECOVERED'
    same_schema_update_qualified = $true
    cross_package_schema_migration_qualified = $false
    real_money_execution = $false
    human_tested = $false
    nvda_verified = $false
    whole_product_complete = $false
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
