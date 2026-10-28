param(
    [Parameter(Mandatory = $true)][string]$PackagePath,
    [Parameter(Mandatory = $true)][string]$PackagedDataExe,
    [Parameter(Mandatory = $true)][string]$FreshDataExe,
    [Parameter(Mandatory = $true)][string]$SourceSha,
    [Parameter(Mandatory = $true)][string]$ExpectedPackageSha256,
    [Parameter(Mandatory = $true)][string]$EvidencePath
)

$ErrorActionPreference = 'Stop'

function Require-CanonicalHex {
    param([string]$Value, [int]$Length, [string]$Label)
    if (
        [string]::IsNullOrWhiteSpace($Value) -or
        $Value.Length -ne $Length -or
        $Value -cnotmatch ('^[0-9a-f]{' + $Length + '}$')
    ) {
        throw "$Label is not canonical lowercase hex"
    }
}

function Invoke-ReleaseDataAction {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$ExpectedStatus
    )
    $raw = (& $Executable @Arguments 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged release lifecycle action failed closed unexpectedly"
    }
    try {
        $record = $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Packaged release lifecycle action emitted noncanonical evidence"
    }
    if (
        [string]$record.status -ne $ExpectedStatus -or
        [string]$record.source_sha -ne $SourceSha -or
        [string]$record.package_sha256 -ne $ExpectedPackageSha256 -or
        [int]$record.file_count -le 0 -or
        $record.real_money_execution -ne $false
    ) {
        throw "Packaged release lifecycle action identity mismatch"
    }
    if ($ExpectedStatus -eq 'VERIFIED' -or $ExpectedStatus -eq 'UNINSTALLED') {
        if ($record.human_tested -ne $false -or $record.nvda_verified -ne $false) {
            throw "Packaged release lifecycle action violated truth labels"
        }
    }
    return $record
}

Require-CanonicalHex -Value $SourceSha -Length 40 -Label 'source SHA'
Require-CanonicalHex -Value $ExpectedPackageSha256 -Length 64 -Label 'package SHA-256'

$package = [System.IO.Path]::GetFullPath($PackagePath)
$packagedTool = [System.IO.Path]::GetFullPath($PackagedDataExe)
$freshTool = [System.IO.Path]::GetFullPath($FreshDataExe)
$evidence = [System.IO.Path]::GetFullPath($EvidencePath)
foreach ($required in @($package, $packagedTool, $freshTool)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw 'Packaged release lifecycle input is missing'
    }
}
$packagedToolSha = (Get-FileHash -LiteralPath $packagedTool -Algorithm SHA256).Hash.ToLowerInvariant()
$freshToolSha = (Get-FileHash -LiteralPath $freshTool -Algorithm SHA256).Hash.ToLowerInvariant()
if ($packagedToolSha -cne $freshToolSha) {
    throw 'Packaged release lifecycle executable copies do not match'
}
if (-not (Test-Path -LiteralPath (Split-Path -Parent $evidence) -PathType Container)) {
    throw 'Packaged release lifecycle evidence parent is missing'
}
if (Test-Path -LiteralPath $evidence) {
    throw 'Packaged release lifecycle evidence destination already exists'
}

$actualPackageSha = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualPackageSha -cne $ExpectedPackageSha256) {
    throw 'Packaged release lifecycle package digest mismatch'
}

$sandbox = Join-Path (Split-Path -Parent $evidence) 'plan6-6a-lifecycle-пробіл-smoke'
$destination = Join-Path $sandbox 'Встановлений Автоспорт'
if (Test-Path -LiteralPath $sandbox) {
    throw 'Packaged release lifecycle sandbox already exists'
}
New-Item -ItemType Directory -Path $sandbox | Out-Null

$common = @(
    '--archive', $package,
    '--destination', $destination,
    '--source-sha', $SourceSha,
    '--package-sha256', $ExpectedPackageSha256
)

try {
    $firstInstall = Invoke-ReleaseDataAction -Executable $packagedTool -Arguments (@('install-release') + $common) -ExpectedStatus 'INSTALLED'
    $firstVerify = Invoke-ReleaseDataAction -Executable $freshTool -Arguments (@('verify-installed-release') + $common) -ExpectedStatus 'VERIFIED'

    # A repeated clean install must fail closed and leave the exact installed
    # release readable. Never treat idempotent verification as overwrite authority.
    $duplicateOutput = (& $freshTool install-release @common 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 3 -or $duplicateOutput -cne 'install=FAIL_CLOSED reason=unsafe_or_unverified_release') {
        throw 'Occupied-destination install did not fail closed canonically'
    }
    $postDuplicateVerify = Invoke-ReleaseDataAction -Executable $packagedTool -Arguments (@('verify-installed-release') + $common) -ExpectedStatus 'VERIFIED'

    $firstUninstall = Invoke-ReleaseDataAction -Executable $freshTool -Arguments (@('uninstall-release') + $common) -ExpectedStatus 'UNINSTALLED'
    if (Test-Path -LiteralPath $destination) {
        throw 'Verified uninstall retained the installed destination'
    }

    $secondInstall = Invoke-ReleaseDataAction -Executable $freshTool -Arguments (@('install-release') + $common) -ExpectedStatus 'INSTALLED'
    $secondVerify = Invoke-ReleaseDataAction -Executable $packagedTool -Arguments (@('verify-installed-release') + $common) -ExpectedStatus 'VERIFIED'
    $secondUninstall = Invoke-ReleaseDataAction -Executable $packagedTool -Arguments (@('uninstall-release') + $common) -ExpectedStatus 'UNINSTALLED'
    if (Test-Path -LiteralPath $destination) {
        throw 'Reinstalled product remained after verified final uninstall'
    }
    if (@(Get-ChildItem -LiteralPath $sandbox -Force).Count -ne 0) {
        throw 'Packaged release lifecycle left an unexpected quarantine artifact'
    }

    $report = [ordered]@{
        schema_version = 1
        evidence_class = 'EXACT_WINDOWS_PACKAGED_RELEASE_LIFECYCLE'
        status = 'PASS'
        source_sha = $SourceSha
        package_sha256 = $ExpectedPackageSha256
        first_install_status = [string]$firstInstall.status
        first_verify_status = [string]$firstVerify.status
        occupied_destination_rejected = $true
        post_rejection_verify_status = [string]$postDuplicateVerify.status
        first_uninstall_status = [string]$firstUninstall.status
        reinstall_status = [string]$secondInstall.status
        reinstall_verify_status = [string]$secondVerify.status
        final_uninstall_status = [string]$secondUninstall.status
        schema_migration_qualified = $false
        real_money_execution = $false
        human_tested = $false
        nvda_verified = $false
        whole_product_complete = $false
    }
    $json = $report | ConvertTo-Json -Depth 3
    [System.IO.File]::WriteAllText(
        $evidence,
        $json + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
} finally {
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -LiteralPath $sandbox -Recurse -Force
    }
}

Write-Output "packaged-release-lifecycle=PASS source_sha=$SourceSha package_sha256=$ExpectedPackageSha256"
