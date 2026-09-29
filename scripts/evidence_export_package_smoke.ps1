$ErrorActionPreference = 'Stop'
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Require-CanonicalHex {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][int]$Length,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($Value -notmatch "^[0-9a-f]{$Length}$") {
        throw "$Label is not canonical lowercase hex"
    }
}

function Invoke-DataTool {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
        throw "$Label executable is missing: $Exe"
    }
    $captured = (& $Exe @Arguments 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $captured
    }
}

function Assert-EvidenceManifestContract {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$RawManifest,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$SecretSentinel
    )

    $expectedPaths = @('decisions.jsonl', 'paper_book.json', 'run_registry.json', 'source_health.json')
    $expectedManifestKeys = @(
        'schema_version',
        'kind',
        'file_count',
        'files',
        'expected_fixed_evidence_paths',
        'missing_fixed_evidence_paths',
        'fixed_evidence_set_complete',
        'run_summary_count',
        'file_contents_included',
        'market_database_included',
        'raw_historical_or_provider_bytes_included',
        'environment_or_credential_values_included',
        'arbitrary_workspace_files_included',
        'real_money_execution',
        'manifest_sha256'
    )
    $actualManifestKeys = @($Manifest.PSObject.Properties.Name)
    if (
        $actualManifestKeys.Count -ne $expectedManifestKeys.Count -or
        (Compare-Object -ReferenceObject $expectedManifestKeys -DifferenceObject $actualManifestKeys)
    ) {
        throw "$Label manifest fields do not match schema version 1"
    }

    if ((($Manifest.schema_version -isnot [int]) -and ($Manifest.schema_version -isnot [long])) -or $Manifest.schema_version -ne 1) {
        throw "$Label manifest schema_version must be exact integer 1"
    }
    if (($Manifest.kind -isnot [string]) -or $Manifest.kind -ne 'autosport-workspace-evidence-manifest') {
        throw "$Label manifest kind mismatch"
    }
    if (($Manifest.file_count -isnot [int]) -and ($Manifest.file_count -isnot [long])) {
        throw "$Label manifest file_count must be an integer"
    }
    if ([long]$Manifest.file_count -ne $expectedPaths.Count) {
        throw "$Label canonical evidence file count mismatch"
    }
    if ($Manifest.files -isnot [System.Array]) {
        throw "$Label manifest files must be an array"
    }
    if ($Manifest.expected_fixed_evidence_paths -isnot [System.Array]) {
        throw "$Label expected fixed evidence paths must be an array"
    }
    $manifestExpectedPaths = @($Manifest.expected_fixed_evidence_paths)
    if (
        $manifestExpectedPaths.Count -ne $expectedPaths.Count -or
        ($manifestExpectedPaths -join "`n") -ne ($expectedPaths -join "`n")
    ) {
        throw "$Label expected fixed evidence paths mismatch"
    }
    if ($Manifest.missing_fixed_evidence_paths -isnot [System.Array]) {
        throw "$Label missing fixed evidence paths must be an array"
    }
    if (@($Manifest.missing_fixed_evidence_paths).Count -ne 0) {
        throw "$Label missing fixed evidence paths must be empty"
    }
    if (($Manifest.fixed_evidence_set_complete -isnot [bool]) -or (-not $Manifest.fixed_evidence_set_complete)) {
        throw "$Label fixed evidence set is incomplete"
    }
    if ((($Manifest.run_summary_count -isnot [int]) -and ($Manifest.run_summary_count -isnot [long])) -or $Manifest.run_summary_count -ne 0) {
        throw "$Label unexpected run summary count"
    }

    foreach ($field in @(
        'file_contents_included',
        'market_database_included',
        'raw_historical_or_provider_bytes_included',
        'environment_or_credential_values_included',
        'arbitrary_workspace_files_included',
        'real_money_execution'
    )) {
        $truthValue = $Manifest.$field
        if (($truthValue -isnot [bool]) -or $truthValue) {
            throw "$Label truth field $field must be exact boolean false"
        }
    }

    $expectedFileRecordKeys = @('path', 'size_bytes', 'sha256')
    $actualPaths = @()
    foreach ($record in @($Manifest.files)) {
        if ($null -eq $record) {
            throw "$Label evidence file record is missing"
        }
        $actualFileRecordKeys = @($record.PSObject.Properties.Name)
        if (
            $actualFileRecordKeys.Count -ne $expectedFileRecordKeys.Count -or
            (Compare-Object -ReferenceObject $expectedFileRecordKeys -DifferenceObject $actualFileRecordKeys)
        ) {
            throw "$Label evidence file record keys mismatch"
        }
        if ($record.path -isnot [string]) {
            throw "$Label evidence file record path must be a string"
        }
        if (($record.size_bytes -isnot [int]) -and ($record.size_bytes -isnot [long])) {
            throw "$Label evidence file record size_bytes must be an integer"
        }
        if ([long]$record.size_bytes -lt 0) {
            throw "$Label evidence file record size_bytes must be non-negative"
        }
        if (($record.sha256 -isnot [string]) -or ($record.sha256 -notmatch '^[0-9a-f]{64}$')) {
            throw "$Label evidence file record sha256 is invalid"
        }
        $actualPaths += $record.path
    }

    if ($actualPaths.Count -ne $expectedPaths.Count) {
        throw "$Label canonical evidence file count mismatch"
    }
    if (($actualPaths -join "`n") -ne ($expectedPaths -join "`n")) {
        throw "$Label canonical evidence path set/order mismatch"
    }
    if ($Manifest.manifest_sha256 -isnot [string]) {
        throw "$Label manifest_sha256 must be a string"
    }
    Require-CanonicalHex -Value $Manifest.manifest_sha256 -Length 64 -Label "$Label manifest_sha256"

    foreach ($forbidden in @($SecretSentinel, 'credential.env', 'market.sqlite', 'raw-provider.bin')) {
        if ($RawManifest.Contains($forbidden)) {
            throw "$Label leaked non-canonical workspace material into metadata-only manifest: $forbidden"
        }
    }
}

function Invoke-EvidenceRoundTrip {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string]$Workspace,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$SecretSentinel
    )

    if (Test-Path -LiteralPath $ManifestPath) { Remove-Item -LiteralPath $ManifestPath -Force }
    $export = Invoke-DataTool -Exe $Exe -Arguments @('export-evidence', $Workspace, '--output', $ManifestPath) -Label $Label
    if ($export.ExitCode -ne 0) { throw "$Label export-evidence exited $($export.ExitCode): $($export.Output)" }
    if ($export.Output -notmatch 'evidence_export=PASS') { throw "$Label export-evidence did not report canonical PASS" }
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { throw "$Label export-evidence did not publish a manifest" }

    $rawManifest = [System.IO.File]::ReadAllText($ManifestPath)
    $manifest = $rawManifest | ConvertFrom-Json
    Assert-EvidenceManifestContract -Manifest $manifest -RawManifest $rawManifest -Label $Label -SecretSentinel $SecretSentinel

    $verify = Invoke-DataTool -Exe $Exe -Arguments @('verify-evidence', $ManifestPath, '--workspace', $Workspace) -Label $Label
    if ($verify.ExitCode -ne 0) { throw "$Label verify-evidence exited $($verify.ExitCode): $($verify.Output)" }
    if ($verify.Output -notmatch 'evidence_verify=PASS' -or $verify.Output -notmatch 'real_money_execution=false') {
        throw "$Label verify-evidence did not report canonical PASS truth"
    }

    return [pscustomobject]@{
        Manifest = $manifest
        FileSha256 = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

$sourceSha = [string]$env:AUTOSPORT_SOURCE_SHA
$packageSha = [string]$env:AUTOSPORT_QUALIFIED_PACKAGE_SHA256
$packagedRoot = [string]$env:AUTOSPORT_PACKAGED_ROOT
$packagedDataExe = [string]$env:AUTOSPORT_PACKAGED_DATA_EXE
Require-CanonicalHex -Value $sourceSha -Length 40 -Label 'AUTOSPORT_SOURCE_SHA'
Require-CanonicalHex -Value $packageSha -Length 64 -Label 'AUTOSPORT_QUALIFIED_PACKAGE_SHA256'
if ([string]::IsNullOrWhiteSpace($packagedRoot)) { throw 'AUTOSPORT_PACKAGED_ROOT is missing' }
if ([string]::IsNullOrWhiteSpace($packagedDataExe)) { throw 'AUTOSPORT_PACKAGED_DATA_EXE is missing' }

$authorityScript = Join-Path $PWD 'scripts/packaged_executable_authority.ps1'
if (-not (Test-Path -LiteralPath $authorityScript -PathType Leaf)) {
    throw "Canonical packaged executable authority helper is missing: $authorityScript"
}
. $authorityScript
$producerIdentity = Get-AutosportProducerPackageIdentity -ExpectedPackageSha256 $packageSha -ExpectedSourceSha $sourceSha
$producerDataExeSha = ([string]$producerIdentity.AutosportDataExeSha256).Trim().ToLowerInvariant()
Require-CanonicalHex -Value $producerDataExeSha -Length 64 -Label 'producer-bound Autosport-Data.exe SHA-256'

$buildInfoPath = Join-Path $packagedRoot 'BUILD_INFO.json'
$freshDataExe = Join-Path $PWD '.build-fresh-extraction/Autosport-V1/Autosport-Data.exe'
$verificationPath = Join-Path $PWD 'dist/fresh-extraction-verification.json'
foreach ($requiredPath in @($buildInfoPath, $packagedDataExe, $freshDataExe, $verificationPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) { throw "Required qualified package input is missing: $requiredPath" }
}

$buildInfo = Get-Content -LiteralPath $buildInfoPath -Raw | ConvertFrom-Json
if ([string]$buildInfo.source_sha -ne $sourceSha) { throw 'BUILD_INFO source_sha does not match exact workflow head' }
if ($buildInfo.real_money_execution -ne $false -or $buildInfo.human_tested -ne $false -or $buildInfo.nvda_verified -ne $false) {
    throw 'BUILD_INFO violated prehuman truth labels'
}
$expectedDataExeSha = ([string]$buildInfo.autosport_data_exe_sha256).Trim().ToLowerInvariant()
Require-CanonicalHex -Value $expectedDataExeSha -Length 64 -Label 'BUILD_INFO autosport_data_exe_sha256'
if ($expectedDataExeSha -ne $producerDataExeSha) {
    throw 'BUILD_INFO Autosport-Data.exe hash is not bound to producer package identity'
}
$packagedDataExeSha = (Get-FileHash -LiteralPath $packagedDataExe -Algorithm SHA256).Hash.ToLowerInvariant()
$freshDataExeSha = (Get-FileHash -LiteralPath $freshDataExe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($packagedDataExeSha -ne $producerDataExeSha) { throw 'Qualified packaged Autosport-Data.exe hash mismatch' }
if ($freshDataExeSha -ne $producerDataExeSha) { throw 'Fresh-extracted Autosport-Data.exe hash mismatch' }

$verification = Get-Content -LiteralPath $verificationPath -Raw | ConvertFrom-Json
if ($verification.status -ne 'PASS') { throw 'Fresh-extraction verification is not PASS' }
if ([string]$verification.source_sha -ne $sourceSha) { throw 'Fresh-extraction verification source_sha mismatch' }
if ([string]$verification.package_sha256 -ne $packageSha) { throw 'Fresh-extraction verification package_sha256 mismatch' }
if (([string]$verification.autosport_data_exe_sha256).Trim().ToLowerInvariant() -ne $producerDataExeSha) { throw 'Fresh-extraction verification Autosport-Data.exe hash mismatch' }
if ($verification.real_money_execution -ne $false -or $verification.human_tested -ne $false -or $verification.nvda_verified -ne $false) {
    throw 'Fresh-extraction verification violated prehuman truth labels'
}

$distRoot = Join-Path $PWD 'dist'
$workspace = Join-Path $distRoot 'evidence-export-package-smoke-workspace'
$packagedManifestPath = Join-Path $distRoot 'evidence-export-package-smoke-packaged.json'
$freshManifestPath = Join-Path $distRoot 'evidence-export-package-smoke-fresh.json'
$resultPath = Join-Path $distRoot 'evidence-export-package-smoke.json'
foreach ($path in @($workspace, $packagedManifestPath, $freshManifestPath, $resultPath)) {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
New-Item -ItemType Directory -Path $workspace | Out-Null

$utf8 = [System.Text.UTF8Encoding]::new($false)
$secretSentinel = 'AUTOSPORT_PACKAGE_SMOKE_SECRET_SENTINEL_DO_NOT_EXPORT_7D8A6B'
[System.IO.File]::WriteAllText((Join-Path $workspace 'decisions.jsonl'), '{"decision":"package-smoke"}' + "`n", $utf8)
[System.IO.File]::WriteAllText((Join-Path $workspace 'paper_book.json'), '{"schema_version":1,"bankroll":"1000.00"}' + "`n", $utf8)
[System.IO.File]::WriteAllText((Join-Path $workspace 'run_registry.json'), '{"schema_version":1,"runs":[]}' + "`n", $utf8)
[System.IO.File]::WriteAllText((Join-Path $workspace 'source_health.json'), '{"schema_version":1,"status":"package-smoke"}' + "`n", $utf8)
[System.IO.File]::WriteAllText((Join-Path $workspace 'credential.env'), $secretSentinel + "`n", $utf8)
[System.IO.File]::WriteAllText((Join-Path $workspace 'market.sqlite'), 'NON_CANONICAL_MARKET_BYTES' + "`n", $utf8)
[System.IO.File]::WriteAllText((Join-Path $workspace 'raw-provider.bin'), 'NON_CANONICAL_PROVIDER_BYTES' + "`n", $utf8)

$packagedAuthority = $null
$freshAuthority = $null
try {
    $packagedAuthority = Open-AutosportQualifiedExecutable -Path $packagedDataExe -ExpectedSha256 $producerDataExeSha -Label 'Packaged evidence-smoke Autosport-Data.exe'
    $freshAuthority = Open-AutosportQualifiedExecutable -Path $freshDataExe -ExpectedSha256 $producerDataExeSha -Label 'Fresh-extracted evidence-smoke Autosport-Data.exe'

    $packagedRoundTrip = Invoke-EvidenceRoundTrip -Exe $packagedDataExe -Workspace $workspace -ManifestPath $packagedManifestPath -Label 'Packaged' -SecretSentinel $secretSentinel
    $freshRoundTrip = Invoke-EvidenceRoundTrip -Exe $freshDataExe -Workspace $workspace -ManifestPath $freshManifestPath -Label 'Fresh-extracted' -SecretSentinel $secretSentinel

    if ([string]$packagedRoundTrip.Manifest.manifest_sha256 -ne [string]$freshRoundTrip.Manifest.manifest_sha256) {
        throw 'Evidence manifest identity changed between qualified packaged and fresh-extracted executables'
    }
    if ($packagedRoundTrip.FileSha256 -ne $freshRoundTrip.FileSha256) {
        throw 'Evidence manifest bytes changed between qualified packaged and fresh-extracted executables'
    }

    [System.IO.File]::AppendAllText((Join-Path $workspace 'paper_book.json'), '{"tamper":true}' + "`n", $utf8)
    $negative = Invoke-DataTool -Exe $packagedDataExe -Arguments @('verify-evidence', $packagedManifestPath, '--workspace', $workspace) -Label 'Packaged tamper probe'
    if ($negative.ExitCode -ne 3) { throw "Tampered workspace verify-evidence must exit 3, got $($negative.ExitCode)" }
    if ($negative.Output -notmatch 'evidence_verify=FAIL_CLOSED') { throw 'Tampered workspace verify-evidence did not fail closed' }

    $result = [ordered]@{
        status = 'PASS'
        source_sha = $sourceSha
        package_sha256 = $packageSha
        autosport_data_exe_sha256 = $producerDataExeSha
        packaged_export_verify_status = 'PASS'
        fresh_extracted_export_verify_status = 'PASS'
        tampered_workspace_verify_status = 'FAIL_CLOSED'
        qualified_executable_authority_held = $true
        manifest_sha256 = [string]$packagedRoundTrip.Manifest.manifest_sha256
        manifest_file_sha256 = $packagedRoundTrip.FileSha256
        fixed_evidence_set_complete = $true
        metadata_only = $true
        secret_sentinel_excluded = $true
        real_money_execution = $false
        human_tested = $false
        nvda_verified = $false
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultPath -Encoding utf8
    Write-Host "evidence_export_package_smoke=PASS source_sha=$sourceSha package_sha256=$packageSha autosport_data_exe_sha256=$producerDataExeSha"
} finally {
    if ($null -ne $freshAuthority) { $freshAuthority.Dispose() }
    if ($null -ne $packagedAuthority) { $packagedAuthority.Dispose() }
    foreach ($path in @($workspace, $packagedManifestPath, $freshManifestPath)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# The tamper probe is expected to leave LASTEXITCODE=3 after its verified fail-closed result.
# All assertions and cleanup have completed successfully here, so normalize the standalone smoke exit.
exit 0
