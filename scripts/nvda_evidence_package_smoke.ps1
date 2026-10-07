$ErrorActionPreference = 'Stop'

$package = Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'
$packagedDataExe = [string]$env:AUTOSPORT_PACKAGED_DATA_EXE
if ([string]::IsNullOrWhiteSpace($packagedDataExe)) { throw 'AUTOSPORT_PACKAGED_DATA_EXE verified package extraction anchor is missing' }
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
$expectedSourceSha = [string]$env:AUTOSPORT_SOURCE_SHA
if ([string]::IsNullOrWhiteSpace($expectedSourceSha)) { throw 'AUTOSPORT_SOURCE_SHA exact-head workflow anchor is missing' }
$expectedSourceSha = $expectedSourceSha.ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($buildInfo.source_sha)) { throw 'BUILD_INFO source_sha is missing' }
if ([string]::IsNullOrWhiteSpace($buildInfo.autosport_exe_sha256)) { throw 'BUILD_INFO Autosport.exe hash is missing' }
if ([string]$buildInfo.source_sha -ne $expectedSourceSha) { throw 'BUILD_INFO source_sha does not match AUTOSPORT_SOURCE_SHA exact-head workflow anchor' }
if ($buildInfo.real_money_execution -ne $false -or $buildInfo.human_tested -ne $false -or $buildInfo.nvda_verified -ne $false) {
  throw 'Release candidate violated prehuman truth labels before NVDA evidence smoke'
}

function Test-NvdaEvidenceContract {
  param(
    [Parameter(Mandatory = $true)][string]$DataExe,
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
    [Parameter(Mandatory = $true)][string]$ExpectedPackageSha256
  )

  $templatePath = Join-Path $PWD "dist/nvda-evidence-$Label-template.json"
  $validationPath = Join-Path $PWD "dist/nvda-evidence-$Label-validation.json"
  foreach ($path in @($templatePath, $validationPath)) {
    if (Test-Path $path) { Remove-Item -Force $path }
  }

  & $DataExe nvda-evidence-template `
    --release-zip $package `
    --expected-source-sha $ExpectedSourceSha `
    --expected-package-sha256 $ExpectedPackageSha256 `
    --output $templatePath | Out-Null
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
  if ($template.candidate.package_sha256 -ne $ExpectedPackageSha256) {
    throw "$Label NVDA template release ZIP SHA-256 mismatch"
  }
  if ($template.candidate.source_sha -ne $ExpectedSourceSha) {
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
    '--expected-source-sha', $ExpectedSourceSha,
    '--expected-package-sha256', $ExpectedPackageSha256,
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
    source_anchor_match_verified = $true
    package_anchor_match_verified = $true
    anchor_provenance_machine_verified = $false
    required_checks_pending = $checks.Count
    untouched_pending_template_rejected = $true
    validator_exit_code = 2
  }
}

# In this CI smoke, source identity comes from GitHub's exact-head workflow context.
# The package SHA below is intentionally self-computed only to exercise the CLI match contract;
# it is NOT evidence that package-hash provenance is independently verified. Physical release
# acceptance must use the separately published package SHA-256 documented in WINDOWS_START_HERE.txt.
$packaged = Test-NvdaEvidenceContract `
  -DataExe $packagedDataExe `
  -Label 'packaged' `
  -ExpectedSourceSha $expectedSourceSha `
  -ExpectedPackageSha256 $packageSha
$fresh = Test-NvdaEvidenceContract `
  -DataExe $freshDataExe `
  -Label 'fresh-extracted' `
  -ExpectedSourceSha $expectedSourceSha `
  -ExpectedPackageSha256 $packageSha
if ($packaged.template_sha256 -ne $fresh.template_sha256) {
  throw 'Packaged and fresh-extracted NVDA evidence templates are not deterministic-identical'
}
if ($packaged.required_checks_pending -ne $fresh.required_checks_pending) {
  throw 'Packaged and fresh-extracted NVDA evidence templates disagree on required pending checks'
}

$evidence = [ordered]@{
  schema_version = 1
  kind = 'nvda_evidence_package_smoke'
  status = 'PASS'
  source_sha = $expectedSourceSha
  release_zip_sha256 = $packageSha
  autosport_exe_sha256 = $buildInfo.autosport_exe_sha256
  source_anchor_match_verified = $true
  source_anchor_workflow_context = 'github_actions_exact_head'
  package_anchor_match_verified = $true
  package_anchor_input_scope = 'self_computed_contract_smoke_only'
  package_anchor_external_provenance_verified = $false
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

# Bind the machine NVDA prerequisite into the canonical fresh-extraction aggregate.
# The aggregate is anchored to workflow-supplied source identity, not to a source SHA
# learned only from the ZIP. CI's package digest remains self-computed contract smoke,
# so package-anchor external provenance stays explicitly unverified.
$verificationPath = Join-Path $PWD 'dist/fresh-extraction-verification.json'
if (-not (Test-Path $verificationPath -PathType Leaf)) {
  throw 'Canonical fresh-extraction verification is missing before NVDA evidence aggregation'
}
$verification = Get-Content $verificationPath -Raw | ConvertFrom-Json
if ($verification.status -ne 'PASS') {
  throw 'Canonical fresh-extraction verification is not PASS before NVDA evidence aggregation'
}
if ($verification.source_sha -ne $expectedSourceSha -or $verification.package_sha256 -ne $packageSha) {
  throw 'Canonical fresh-extraction verification identity drifted from externally anchored NVDA candidate'
}
$sourceAnchorMatches = (
  $packaged.source_anchor_match_verified -eq $true -and
  $fresh.source_anchor_match_verified -eq $true -and
  $evidence.source_anchor_match_verified -eq $true
)
$packageAnchorMatches = (
  $packaged.package_anchor_match_verified -eq $true -and
  $fresh.package_anchor_match_verified -eq $true -and
  $evidence.package_anchor_match_verified -eq $true
)
if (-not $sourceAnchorMatches -or -not $packageAnchorMatches) {
  throw 'NVDA candidate anchor equality did not verify before aggregate publication'
}
if ($packaged.anchor_provenance_machine_verified -ne $false -or
    $fresh.anchor_provenance_machine_verified -ne $false -or
    $evidence.package_anchor_external_provenance_verified -ne $false) {
  throw 'NVDA aggregate must not promote self-computed package-anchor provenance to independently verified truth'
}

$verification | Add-Member -NotePropertyName extracted_nvda_evidence_contract_status -NotePropertyValue $evidence.status -Force
$verification | Add-Member -NotePropertyName extracted_nvda_candidate_identity_verified -NotePropertyValue ($packaged.candidate_identity_verified -and $fresh.candidate_identity_verified) -Force
$verification | Add-Member -NotePropertyName extracted_nvda_source_anchor_match_verified -NotePropertyValue $sourceAnchorMatches -Force
$verification | Add-Member -NotePropertyName extracted_nvda_package_anchor_match_verified -NotePropertyValue $packageAnchorMatches -Force
$verification | Add-Member -NotePropertyName extracted_nvda_anchor_provenance_machine_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName extracted_nvda_source_anchor_workflow_context -NotePropertyValue $evidence.source_anchor_workflow_context -Force
$verification | Add-Member -NotePropertyName extracted_nvda_package_anchor_input_scope -NotePropertyValue $evidence.package_anchor_input_scope -Force
$verification | Add-Member -NotePropertyName extracted_nvda_package_anchor_external_provenance_verified -NotePropertyValue $evidence.package_anchor_external_provenance_verified -Force
$verification | Add-Member -NotePropertyName extracted_nvda_required_checks_pending -NotePropertyValue $packaged.required_checks_pending -Force
$verification | Add-Member -NotePropertyName extracted_nvda_untouched_pending_template_rejected -NotePropertyValue ($packaged.untouched_pending_template_rejected -and $fresh.untouched_pending_template_rejected) -Force
$verification | Add-Member -NotePropertyName extracted_nvda_machine_verified_physical_execution -NotePropertyValue $evidence.machine_verified_physical_execution -Force
$verification | Add-Member -NotePropertyName extracted_nvda_template_sha256 -NotePropertyValue $packaged.template_sha256 -Force
$verification | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $verificationPath -Encoding utf8

Write-Host "NVDA_EVIDENCE_PACKAGE_SMOKE=PASS package_sha256=$packageSha source_sha=$expectedSourceSha package_anchor_external_provenance_verified=false"
