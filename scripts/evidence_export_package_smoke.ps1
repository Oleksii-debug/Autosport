Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$packagedDataExe = [string]$env:AUTOSPORT_PACKAGED_DATA_EXE
$freshDataExe = Join-Path $PWD '.build-fresh-extraction/Autosport-V1/Autosport-Data.exe'
$sourceSha = [string]$env:AUTOSPORT_SOURCE_SHA

if ([string]::IsNullOrWhiteSpace($packagedDataExe) -or -not (Test-Path -LiteralPath $packagedDataExe -PathType Leaf)) {
  throw 'Packaged evidence export smoke is missing verified packaged Autosport-Data.exe'
}
if (-not (Test-Path -LiteralPath $freshDataExe -PathType Leaf)) {
  throw 'Packaged evidence export smoke is missing fresh-extracted Autosport-Data.exe'
}
if ($sourceSha -notmatch '^[0-9a-f]{40}$') {
  throw 'Packaged evidence export smoke is missing canonical exact source SHA'
}

function Invoke-DataTool {
  param(
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string[]]$Arguments,
    [Parameter(Mandatory=$true)][string]$Label,
    [Parameter(Mandatory=$true)][int[]]$ExpectedExitCodes
  )

  $output = (& $Exe @Arguments 2>&1 | Out-String).Trim()
  $exitCode = $LASTEXITCODE
  if ($ExpectedExitCodes -notcontains $exitCode) {
    throw "$Label exited $exitCode; expected one of $($ExpectedExitCodes -join ', '): $output"
  }
  return [pscustomobject]@{
    ExitCode = $exitCode
    Output = $output
  }
}

$smokeRoot = Join-Path $env:RUNNER_TEMP 'autosport-evidence-export-package-smoke'
$workspace = Join-Path $smokeRoot 'workspace'
$packagedManifest = Join-Path $smokeRoot 'packaged-evidence-manifest.json'
$freshManifest = Join-Path $smokeRoot 'fresh-evidence-manifest.json'
$evidencePath = Join-Path $PWD 'dist/evidence-export-package-smoke.json'

try {
  if (Test-Path -LiteralPath $smokeRoot) {
    Remove-Item -LiteralPath $smokeRoot -Recurse -Force
  }
  New-Item -ItemType Directory -Path $workspace | Out-Null

  # Canonical metadata inputs are intentionally tiny and synthetic. This smoke proves
  # packaged command reachability/determinism/fail-closed verification only; it makes
  # no claim about historical coverage, human testing, NVDA testing, or real money.
  [System.IO.File]::WriteAllText((Join-Path $workspace 'decisions.jsonl'), "{`"kind`":`"smoke`"}`n", [System.Text.UTF8Encoding]::new($false))
  [System.IO.File]::WriteAllText((Join-Path $workspace 'paper_book.json'), "{`"balance`":`"1000`"}`n", [System.Text.UTF8Encoding]::new($false))
  [System.IO.File]::WriteAllText((Join-Path $workspace 'run_registry.json'), "{}`n", [System.Text.UTF8Encoding]::new($false))
  [System.IO.File]::WriteAllText((Join-Path $workspace 'source_health.json'), "{`"status`":`"smoke`"}`n", [System.Text.UTF8Encoding]::new($false))

  $packagedExport = Invoke-DataTool -Exe $packagedDataExe -Arguments @('export-evidence', $workspace, '--output', $packagedManifest) -Label 'Packaged export-evidence' -ExpectedExitCodes @(0)
  if ($packagedExport.Output -notmatch 'evidence_export=PASS') {
    throw 'Packaged export-evidence did not report evidence_export=PASS'
  }
  if (-not (Test-Path -LiteralPath $packagedManifest -PathType Leaf)) {
    throw 'Packaged export-evidence did not publish manifest'
  }

  $freshVerifyPackaged = Invoke-DataTool -Exe $freshDataExe -Arguments @('verify-evidence', $packagedManifest, '--workspace', $workspace) -Label 'Fresh verify-evidence of packaged manifest' -ExpectedExitCodes @(0)
  if ($freshVerifyPackaged.Output -notmatch 'evidence_verify=PASS') {
    throw 'Fresh verify-evidence did not report evidence_verify=PASS for packaged manifest'
  }

  $freshExport = Invoke-DataTool -Exe $freshDataExe -Arguments @('export-evidence', $workspace, '--output', $freshManifest) -Label 'Fresh export-evidence' -ExpectedExitCodes @(0)
  if ($freshExport.Output -notmatch 'evidence_export=PASS') {
    throw 'Fresh export-evidence did not report evidence_export=PASS'
  }
  if (-not (Test-Path -LiteralPath $freshManifest -PathType Leaf)) {
    throw 'Fresh export-evidence did not publish manifest'
  }

  $packagedVerifyFresh = Invoke-DataTool -Exe $packagedDataExe -Arguments @('verify-evidence', $freshManifest, '--workspace', $workspace) -Label 'Packaged verify-evidence of fresh manifest' -ExpectedExitCodes @(0)
  if ($packagedVerifyFresh.Output -notmatch 'evidence_verify=PASS') {
    throw 'Packaged verify-evidence did not report evidence_verify=PASS for fresh manifest'
  }

  $packagedManifestSha = (Get-FileHash -LiteralPath $packagedManifest -Algorithm SHA256).Hash.ToLowerInvariant()
  $freshManifestSha = (Get-FileHash -LiteralPath $freshManifest -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($packagedManifestSha -ne $freshManifestSha) {
    throw "Packaged/fresh evidence export is not deterministic: $packagedManifestSha != $freshManifestSha"
  }

  # Adversarial proof: canonical evidence drift after export must not be accepted.
  [System.IO.File]::AppendAllText((Join-Path $workspace 'decisions.jsonl'), "{`"kind`":`"tamper-probe`"}`n", [System.Text.UTF8Encoding]::new($false))
  $tamperVerify = Invoke-DataTool -Exe $packagedDataExe -Arguments @('verify-evidence', $packagedManifest, '--workspace', $workspace) -Label 'Packaged verify-evidence tamper probe' -ExpectedExitCodes @(3)
  if ($tamperVerify.Output -notmatch 'evidence_verify=FAIL_CLOSED') {
    throw 'Packaged verify-evidence tamper probe did not fail closed'
  }

  $evidence = [ordered]@{
    status = 'PASS'
    source_sha = $sourceSha
    packaged_export_status = 'PASS'
    fresh_verify_packaged_manifest_status = 'PASS'
    fresh_export_status = 'PASS'
    packaged_verify_fresh_manifest_status = 'PASS'
    deterministic_manifest_sha256 = $packagedManifestSha
    tamper_verification_rejected = $true
    metadata_only_command_contract = $true
    real_money_execution = $false
    human_tested = $false
    nvda_verified = $false
  }
  $evidence | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $evidencePath -Encoding utf8
} finally {
  Remove-Item -LiteralPath $smokeRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$published = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
if ($published.status -ne 'PASS' -or $published.tamper_verification_rejected -ne $true) {
  throw 'Packaged evidence export smoke did not publish PASS evidence'
}
if ($published.real_money_execution -ne $false -or $published.human_tested -ne $false -or $published.nvda_verified -ne $false) {
  throw 'Packaged evidence export smoke violated release truth labels'
}

Write-Host "evidence_export_package_smoke=PASS manifest_sha256=$($published.deterministic_manifest_sha256)"
