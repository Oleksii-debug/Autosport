$ErrorActionPreference = 'Stop'

$package = Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'
$packagedDataExe = Join-Path $PWD 'dist/Autosport-Data.exe'
$packageRoot = Join-Path $PWD '.build-fresh-extraction/Autosport-V1'
$freshDataExe = Join-Path $packageRoot 'Autosport-Data.exe'
$buildInfoPath = Join-Path $packageRoot 'BUILD_INFO.json'

foreach ($required in @($package, $packagedDataExe, $freshDataExe, $buildInfoPath)) {
  if (-not (Test-Path $required -PathType Leaf)) {
    throw "NVDA evidence package smoke is missing required artifact: $required"
  }
}

$buildInfo = Get-Content $buildInfoPath -Raw | ConvertFrom-Json
$packageSha = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($buildInfo.source_sha)) { throw 'BUILD_INFO source_sha is missing' }
if ([string]::IsNullOrWhiteSpace($buildInfo.autosport_exe_sha256)) { throw 'BUILD_INFO Autosport.exe hash is missing' }
if ($buildInfo.real_money_execution -ne $false -or $buildInfo.human_tested -ne $false -or $buildInfo.nvda_verified -ne $false) {
  throw 'Release candidate violated prehuman truth labels before NVDA evidence smoke'
}

function Test-NvdaEvidenceContract {
  param(
    [Parameter(Mandatory = $true)][string]$DataExe,
    [Parameter(Mandatory = $true)][string]$Label
  )

  $templatePath = Join-Path $PWD "dist/nvda-evidence-$Label-template.json"
  $validationPath = Join-Path $PWD "dist/nvda-evidence-$Label-validation.json"
  foreach ($path in @($templatePath, $validationPath)) {
    if (Test-Path $path) { Remove-Item -Force $path }
  }

  & $DataExe nvda-evidence-template --release-zip $package --output $templatePath | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "$Label Autosport-Data.exe nvda-evidence-template exited $LASTEXITCODE"
  }
  if (-not (Test-Path $templatePath -PathType Leaf)) {
    throw "$Label nvda-evidence-template did not create evidence template"
  }

  $template = Get-Content $templatePath -Raw | ConvertFrom-Json
  if ($template.schema_version -ne 1 -or $template.kind -ne 'physical_nvda_acceptance') {
    throw "$Label NVDA evidence template schema/kind mismatch"
  }
  if ($template.candidate.package_sha256 -ne $packageSha) {
    throw "$Label NVDA template release ZIP SHA-256 mismatch"
  }
  if ($template.candidate.source_sha -ne $buildInfo.source_sha) {
    throw "$Label NVDA template source_sha mismatch"
  }
  if ($template.candidate.autosport_exe_sha256 -ne $buildInfo.autosport_exe_sha256) {
    throw "$Label NVDA template Autosport.exe SHA-256 mismatch"
  }
  if ($template.real_money_execution -ne $false -or $template.human_tested -ne $false -or $template.nvda_verified -ne $false -or $template.v1_ready -ne $false) {
    throw "$Label machine-generated NVDA template promoted a forbidden truth flag"
  }
  if (-not [string]::IsNullOrEmpty([string]$template.environment.windows_edition_build) -or
      -not [string]::IsNullOrEmpty([string]$template.environment.nvda_version) -or
      -not [string]::IsNullOrEmpty([string]$template.tested_at) -or
      -not [string]::IsNullOrEmpty([string]$template.tester_label)) {
    throw "$Label machine-generated NVDA template fabricated human environment/test identity"
  }
  $checks = @($template.checks)
  if ($checks.Count -eq 0) { throw "$Label NVDA template has no required physical checks" }
  if (@($checks | Where-Object { $_.status -ne 'PENDING' }).Count -ne 0) {
    throw "$Label machine-generated NVDA template must leave every physical check PENDING"
  }

  # The untouched machine-generated template is deliberately incomplete human evidence.
  # Exercise the frozen validator and require fail-closed rejection rather than filling
  # PASS values in CI, which would fabricate physical Windows/NVDA evidence.
  $verify = Start-Process -FilePath $DataExe -ArgumentList @(
    'verify-nvda-evidence', '--release-zip', $package,
    '--evidence', $templatePath, '--output', $validationPath
  ) -Wait -PassThru
  if ($verify.ExitCode -eq 0) {
    throw "$Label NVDA validator accepted untouched PENDING machine evidence"
  }
  if ($verify.ExitCode -ne 2) {
    throw "$Label NVDA validator returned unexpected fail-closed exit $($verify.ExitCode)"
  }
  if (Test-Path $validationPath -PathType Leaf) {
    throw "$Label NVDA validator wrote a success-shaped report for incomplete human evidence"
  }

  return [ordered]@{
    label = $Label
    template_path = (Split-Path $templatePath -Leaf)
    template_sha256 = (Get-FileHash -LiteralPath $templatePath -Algorithm SHA256).Hash.ToLowerInvariant()
    candidate_identity_verified = $true
    required_checks_pending = $checks.Count
    untouched_pending_template_rejected = $true
    validator_exit_code = 2
  }
}

$packaged = Test-NvdaEvidenceContract -DataExe $packagedDataExe -Label 'packaged'
$fresh = Test-NvdaEvidenceContract -DataExe $freshDataExe -Label 'fresh-extracted'
if ($packaged.template_sha256 -ne $fresh.template_sha256) {
  throw 'Packaged and fresh-extracted NVDA evidence templates are not deterministic-identical'
}

$evidence = [ordered]@{
  schema_version = 1
  kind = 'nvda_evidence_package_smoke'
  status = 'PASS'
  source_sha = $buildInfo.source_sha
  release_zip_sha256 = $packageSha
  autosport_exe_sha256 = $buildInfo.autosport_exe_sha256
  packaged = $packaged
  fresh_extracted = $fresh
  machine_verified_physical_execution = $false
  human_tested = $false
  nvda_verified = $false
  v1_ready = $false
  real_money_execution = $false
}
$evidencePath = Join-Path $PWD 'dist/nvda-evidence-package-smoke.json'
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $evidencePath -Encoding utf8
Write-Host "NVDA_EVIDENCE_PACKAGE_SMOKE=PASS package_sha256=$packageSha source_sha=$($buildInfo.source_sha)"
