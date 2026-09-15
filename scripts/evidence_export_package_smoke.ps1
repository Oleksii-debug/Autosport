param(
    [string]$PackagedDataExe = (Join-Path $PWD 'dist/Autosport-Data.exe'),
    [string]$FreshDataExe = (Join-Path $PWD '.build-fresh-extraction/Autosport-V1/Autosport-Data.exe'),
    [string]$OutputRoot = (Join-Path $PWD 'dist/evidence-export-package-smoke')
)

$ErrorActionPreference = 'Stop'

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    [System.IO.File]::WriteAllText(
        $Path,
        $Text,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Initialize-EvidenceWorkspace {
    param([Parameter(Mandatory = $true)][string]$Workspace)

    New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
    $fixtures = [ordered]@{
        'decisions.jsonl' = '{"decision":"package-smoke","mode":"paper"}' + "`n"
        'paper_book.json' = '{"balance":"1000.00","currency":"TEST","mode":"paper","private_note":"package-smoke-content-sentinel"}' + "`n"
        'run_registry.json' = '{"runs":[]}' + "`n"
        'source_health.json' = '{"status":"package-smoke"}' + "`n"
        'run-00000000-0000-4000-8000-000000000001.json' = '{"run_id":"00000000-0000-4000-8000-000000000001","status":"package-smoke"}' + "`n"
    }
    foreach ($entry in $fixtures.GetEnumerator()) {
        Write-Utf8NoBom -Path (Join-Path $Workspace $entry.Key) -Text $entry.Value
    }
}

function Invoke-DataTool {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $lines = @(& $Exe @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $text = ($lines | ForEach-Object { $_.ToString() }) -join "`n"
    [pscustomobject]@{
        Label = $Label
        ExitCode = $exitCode
        Output = $text
    }
}

function Assert-SuccessfulExport {
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][string]$Manifest
    )
    if ($Result.ExitCode -ne 0) {
        throw "$($Result.Label) export failed with exit code $($Result.ExitCode): $($Result.Output)"
    }
    if ($Result.Output -notmatch 'evidence_export=PASS') {
        throw "$($Result.Label) export did not report evidence_export=PASS"
    }
    if (-not (Test-Path $Manifest -PathType Leaf)) {
        throw "$($Result.Label) export did not create the manifest"
    }
}

function Assert-SuccessfulVerify {
    param([Parameter(Mandatory = $true)]$Result)
    if ($Result.ExitCode -ne 0) {
        throw "$($Result.Label) verify failed with exit code $($Result.ExitCode): $($Result.Output)"
    }
    if ($Result.Output -notmatch 'evidence_verify=PASS') {
        throw "$($Result.Label) verify did not report evidence_verify=PASS"
    }
}

function Assert-ManifestTruth {
    param(
        [Parameter(Mandatory = $true)][string]$Manifest,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $report = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
    if ($report.schema_version -ne 1) { throw "$Label manifest schema_version mismatch" }
    if ($report.kind -ne 'autosport-workspace-evidence-manifest') { throw "$Label manifest kind mismatch" }
    if ($report.file_count -ne 5) { throw "$Label manifest file_count mismatch" }
    if ($report.run_summary_count -ne 1) { throw "$Label manifest run_summary_count mismatch" }
    if ($report.fixed_evidence_set_complete -ne $true) { throw "$Label manifest fixed evidence set is incomplete" }
    if (@($report.missing_fixed_evidence_paths).Count -ne 0) { throw "$Label manifest reports missing fixed evidence" }

    $expectedPaths = @(
        'decisions.jsonl',
        'paper_book.json',
        'run-00000000-0000-4000-8000-000000000001.json',
        'run_registry.json',
        'source_health.json'
    )
    $actualPaths = @($report.files | ForEach-Object { $_.path })
    if (($actualPaths -join "`n") -ne ($expectedPaths -join "`n")) {
        throw "$Label manifest canonical path set/order mismatch"
    }

    foreach ($field in @(
        'file_contents_included',
        'market_database_included',
        'raw_historical_or_provider_bytes_included',
        'environment_or_credential_values_included',
        'arbitrary_workspace_files_included',
        'real_money_execution'
    )) {
        if ($report.$field -ne $false) {
            throw "$Label manifest truth field must remain false: $field"
        }
    }
    $manifestText = Get-Content -LiteralPath $Manifest -Raw
    if ($manifestText -match 'package-smoke-content-sentinel') {
        throw "$Label manifest leaked canonical evidence file contents"
    }
    if ($report.manifest_sha256 -notmatch '^[0-9a-f]{64}$') {
        throw "$Label manifest_sha256 is invalid"
    }
    return $report
}

foreach ($candidate in @(
    @{ Path = $PackagedDataExe; Label = 'packaged' },
    @{ Path = $FreshDataExe; Label = 'fresh-extracted' }
)) {
    if (-not (Test-Path $candidate.Path -PathType Leaf)) {
        throw "$($candidate.Label) Autosport-Data.exe is missing: $($candidate.Path)"
    }
}

$packagedResolved = (Resolve-Path -LiteralPath $PackagedDataExe).Path
$freshResolved = (Resolve-Path -LiteralPath $FreshDataExe).Path
if ($packagedResolved -eq $freshResolved) {
    throw 'Packaged and fresh-extracted Autosport-Data.exe must be distinct files'
}
$packagedExeHash = (Get-FileHash -LiteralPath $packagedResolved -Algorithm SHA256).Hash.ToLowerInvariant()
$freshExeHash = (Get-FileHash -LiteralPath $freshResolved -Algorithm SHA256).Hash.ToLowerInvariant()
if ($packagedExeHash -ne $freshExeHash) {
    throw 'Packaged and fresh-extracted Autosport-Data.exe SHA-256 mismatch'
}

if (Test-Path $OutputRoot) { Remove-Item -Recurse -Force $OutputRoot }
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

$packagedWorkspace = Join-Path $OutputRoot 'packaged-workspace'
$freshWorkspace = Join-Path $OutputRoot 'fresh-workspace'
$packagedManifest = Join-Path $OutputRoot 'packaged-manifest.json'
$freshManifest = Join-Path $OutputRoot 'fresh-manifest.json'
$summaryPath = Join-Path $OutputRoot 'evidence-export-package-smoke.json'

Initialize-EvidenceWorkspace -Workspace $packagedWorkspace
Initialize-EvidenceWorkspace -Workspace $freshWorkspace

$nativePreference = Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
$previousNativePreference = $null
if ($null -ne $nativePreference) {
    $previousNativePreference = $nativePreference.Value
    $PSNativeCommandUseErrorActionPreference = $false
}

try {
    $packagedExport = Invoke-DataTool -Exe $packagedResolved -Arguments @(
        'export-evidence', $packagedWorkspace, '--output', $packagedManifest
    ) -Label 'packaged'
    Assert-SuccessfulExport -Result $packagedExport -Manifest $packagedManifest
    $packagedReport = Assert-ManifestTruth -Manifest $packagedManifest -Label 'packaged'

    $packagedVerify = Invoke-DataTool -Exe $packagedResolved -Arguments @(
        'verify-evidence', $packagedManifest, '--workspace', $packagedWorkspace
    ) -Label 'packaged'
    Assert-SuccessfulVerify -Result $packagedVerify

    $freshExport = Invoke-DataTool -Exe $freshResolved -Arguments @(
        'export-evidence', $freshWorkspace, '--output', $freshManifest
    ) -Label 'fresh-extracted'
    Assert-SuccessfulExport -Result $freshExport -Manifest $freshManifest
    $freshReport = Assert-ManifestTruth -Manifest $freshManifest -Label 'fresh-extracted'

    $freshVerify = Invoke-DataTool -Exe $freshResolved -Arguments @(
        'verify-evidence', $freshManifest, '--workspace', $freshWorkspace
    ) -Label 'fresh-extracted'
    Assert-SuccessfulVerify -Result $freshVerify

    $packagedManifestHash = (Get-FileHash -LiteralPath $packagedManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $freshManifestHash = (Get-FileHash -LiteralPath $freshManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($packagedManifestHash -ne $freshManifestHash) {
        throw 'Packaged and fresh-extracted evidence manifests are not byte-deterministic'
    }
    if ($packagedReport.manifest_sha256 -ne $freshReport.manifest_sha256) {
        throw 'Packaged and fresh-extracted manifest_sha256 values differ'
    }

    [System.IO.File]::AppendAllText(
        (Join-Path $packagedWorkspace 'paper_book.json'),
        '{"tampered":true}' + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    $tamperedVerify = Invoke-DataTool -Exe $packagedResolved -Arguments @(
        'verify-evidence', $packagedManifest, '--workspace', $packagedWorkspace
    ) -Label 'tampered-packaged'
    if ($tamperedVerify.ExitCode -ne 3) {
        throw "Tampered packaged verify must fail closed with exit code 3, got $($tamperedVerify.ExitCode): $($tamperedVerify.Output)"
    }
    if ($tamperedVerify.Output -notmatch 'evidence_verify=FAIL_CLOSED') {
        throw 'Tampered packaged verify did not report evidence_verify=FAIL_CLOSED'
    }

    $summary = [ordered]@{
        schema_version = 1
        status = 'PASS'
        packaged_export_status = 'PASS'
        packaged_verify_status = 'PASS'
        fresh_extracted_export_status = 'PASS'
        fresh_extracted_verify_status = 'PASS'
        executable_sha256_match = $true
        manifest_byte_determinism_verified = $true
        manifest_sha256_match = $true
        metadata_only_truth_fields_verified = $true
        file_content_sentinel_absent = $true
        tampered_workspace_fail_closed_verified = $true
        packaged_executable_sha256 = $packagedExeHash
        evidence_manifest_sha256 = $packagedReport.manifest_sha256
        real_money_execution = $false
        human_tested = $false
        nvda_verified = $false
        v1_ready = $false
    }
    $summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $summaryPath -Encoding utf8
    Write-Host "evidence_export_package_smoke=PASS summary=$summaryPath"
}
finally {
    if ($null -ne $nativePreference) {
        $PSNativeCommandUseErrorActionPreference = $previousNativePreference
    }
}
