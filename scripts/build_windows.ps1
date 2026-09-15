$ErrorActionPreference = 'Stop'

function Assert-ProcessRecoveryEvidence {
  param(
    [Parameter(Mandatory = $true)] $Evidence,
    [Parameter(Mandatory = $true)] [string] $Label
  )

  if ($Evidence.process_kill_relaunch_status -ne 'PASS') {
    throw "$Label did not prove real process kill/relaunch"
  }
  if ($null -eq $Evidence.process_kill_stage_pid -or [long]$Evidence.process_kill_stage_pid -le 0) {
    throw "$Label has invalid process_kill_stage_pid"
  }
  if ($null -eq $Evidence.process_recovery_pid -or [long]$Evidence.process_recovery_pid -le 0) {
    throw "$Label has invalid process_recovery_pid"
  }
  if ([long]$Evidence.process_kill_stage_pid -eq [long]$Evidence.process_recovery_pid) {
    throw "$Label did not prove a distinct fresh recovery process"
  }
  if ($null -eq $Evidence.process_kill_return_code -or [long]$Evidence.process_kill_return_code -eq 0) {
    throw "$Label did not prove non-clean process termination"
  }
  if ([string]::IsNullOrWhiteSpace([string]$Evidence.process_recovery_run_id)) {
    throw "$Label has invalid process_recovery_run_id"
  }
  if ($Evidence.process_recovery_disposition -ne 'committed') {
    throw "$Label did not prove process_recovery_disposition=committed"
  }
  if ($Evidence.process_recovery_registry_status -ne 'completed') {
    throw "$Label did not prove process_recovery_registry_status=completed"
  }
  if ($Evidence.process_recovery_manifest_phase -ne 'completed') {
    throw "$Label did not prove process_recovery_manifest_phase=completed"
  }

  $hashFields = @(
    'process_recovery_base_paper_book_sha256',
    'process_recovery_base_decision_ledger_sha256',
    'process_recovery_new_paper_book_sha256',
    'process_recovery_new_decision_ledger_sha256'
  )
  foreach ($field in $hashFields) {
    $value = [string]$Evidence.$field
    if ($value -notmatch '^[0-9a-f]{64}$') {
      throw "$Label has invalid $field"
    }
  }
  if ($Evidence.process_recovery_base_paper_book_sha256 -eq $Evidence.process_recovery_new_paper_book_sha256) {
    throw "$Label did not prove promoted PaperBook state"
  }
  if ($Evidence.process_recovery_base_decision_ledger_sha256 -eq $Evidence.process_recovery_new_decision_ledger_sha256) {
    throw "$Label did not prove promoted Decision Ledger state"
  }
}

$sourceSha = $env:AUTOSPORT_SOURCE_SHA
if ([string]::IsNullOrWhiteSpace($sourceSha)) { $sourceSha = (git rev-parse HEAD).Trim() }
python scripts/verify_source_checkout.py --source-sha $sourceSha
if ($LASTEXITCODE -ne 0) { throw "Source checkout preflight exited $LASTEXITCODE" }
$sourceVerifier = (New-TemporaryFile).FullName
Copy-Item -LiteralPath 'scripts/verify_source_checkout.py' -Destination $sourceVerifier -Force
$boundArtifactRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("autosport-release-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $boundArtifactRoot | Out-Null
$boundAutosportExe = Join-Path $boundArtifactRoot 'Autosport.exe'
$boundDataExe = Join-Path $boundArtifactRoot 'Autosport-Data.exe'
$boundDiagnostic = Join-Path $boundArtifactRoot 'packaged-diagnostic.json'
$boundAccessibilityAudit = Join-Path $boundArtifactRoot 'accessibility-audit.json'
$boundKeyboardAudit = Join-Path $boundArtifactRoot 'keyboard-audit.json'
$boundRestartRecoveryAudit = Join-Path $boundArtifactRoot 'restart-recovery-audit.json'
$autosportDigestPath = Join-Path $boundArtifactRoot 'Autosport.sha256'
$dataDigestPath = Join-Path $boundArtifactRoot 'Autosport-Data.sha256'
$diagnosticDigestPath = Join-Path $boundArtifactRoot 'packaged-diagnostic.sha256'
$accessibilityDigestPath = Join-Path $boundArtifactRoot 'accessibility-audit.sha256'
$keyboardDigestPath = Join-Path $boundArtifactRoot 'keyboard-audit.sha256'
$restartRecoveryDigestPath = Join-Path $boundArtifactRoot 'restart-recovery-audit.sha256'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade exited $LASTEXITCODE" }
python -m pip install -e '.[build,test]'
if ($LASTEXITCODE -ne 0) { throw "build/test dependency install exited $LASTEXITCODE" }
python -m pytest -v tests
if ($LASTEXITCODE -ne 0) { throw "Full pytest gate exited $LASTEXITCODE" }
if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }
python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace
if ($LASTEXITCODE -ne 0) { throw "Demo dataset smoke exited $LASTEXITCODE" }
if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }
python $sourceVerifier --source-sha $sourceSha --late-build-boundary
if ($LASTEXITCODE -ne 0) { throw "Trusted source gate before Autosport.exe exited $LASTEXITCODE" }
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Autosport src/autosport/windows_entry.py
if ($LASTEXITCODE -ne 0) { throw "Autosport PyInstaller exited $LASTEXITCODE" }
$builtAutosportExe = Join-Path $PWD 'dist/Autosport.exe'
python $sourceVerifier --bind-artifact $builtAutosportExe --bound-output $boundAutosportExe --digest-output $autosportDigestPath
if ($LASTEXITCODE -ne 0) { throw "Autosport.exe artifact binding exited $LASTEXITCODE" }
$autosportExeSha256 = (Get-Content -LiteralPath $autosportDigestPath -Raw).Trim()
python $sourceVerifier --source-sha $sourceSha --late-build-boundary --allow-release-outputs
if ($LASTEXITCODE -ne 0) { throw "Trusted source gate before Autosport-Data.exe exited $LASTEXITCODE" }
python -m PyInstaller --noconfirm --clean --onefile --console --name Autosport-Data src/autosport/data_tools_entry.py
if ($LASTEXITCODE -ne 0) { throw "Autosport-Data PyInstaller exited $LASTEXITCODE" }
$builtDataExe = Join-Path $PWD 'dist/Autosport-Data.exe'
python $sourceVerifier --bind-artifact $builtDataExe --bound-output $boundDataExe --digest-output $dataDigestPath
if ($LASTEXITCODE -ne 0) { throw "Autosport-Data.exe artifact binding exited $LASTEXITCODE" }
$dataExeSha256 = (Get-Content -LiteralPath $dataDigestPath -Raw).Trim()

$diag = Join-Path $PWD 'dist/packaged-diagnostic.json'
if (Test-Path $diag) { Remove-Item -Force $diag }
$process = Start-Process -FilePath $boundAutosportExe -ArgumentList '--diagnostic-output', $diag -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Packaged Autosport.exe diagnostic exited $($process.ExitCode)" }
python $sourceVerifier --bind-artifact $diag --bound-output $boundDiagnostic --digest-output $diagnosticDigestPath
if ($LASTEXITCODE -ne 0) { throw "Packaged diagnostic evidence binding exited $LASTEXITCODE" }
$diagnosticSha256 = (Get-Content -LiteralPath $diagnosticDigestPath -Raw).Trim()
$diagnostic = Get-Content $boundDiagnostic -Raw | ConvertFrom-Json
if ($diagnostic.status -ne 'PASS') { throw 'Packaged Autosport.exe diagnostic did not PASS' }

$a11y = Join-Path $PWD 'dist/accessibility-audit.json'
if (Test-Path $a11y) { Remove-Item -Force $a11y }
$a11yProcess = Start-Process -FilePath $boundAutosportExe -ArgumentList '--accessibility-audit-output', $a11y -Wait -PassThru
if ($a11yProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe accessibility audit exited $($a11yProcess.ExitCode)" }
python $sourceVerifier --bind-artifact $a11y --bound-output $boundAccessibilityAudit --digest-output $accessibilityDigestPath
if ($LASTEXITCODE -ne 0) { throw "Accessibility evidence binding exited $LASTEXITCODE" }
$accessibilitySha256 = (Get-Content -LiteralPath $accessibilityDigestPath -Raw).Trim()
$accessibility = Get-Content $boundAccessibilityAudit -Raw | ConvertFrom-Json
if ($accessibility.status -ne 'PASS') { throw 'Packaged accessibility audit did not PASS' }
if ($accessibility.nvda_verified -ne $false) { throw 'Machine accessibility audit must not claim NVDA verification' }

$keyboard = Join-Path $PWD 'dist/keyboard-audit.json'
if (Test-Path $keyboard) { Remove-Item -Force $keyboard }
$keyboardProcess = Start-Process -FilePath $boundAutosportExe -ArgumentList '--keyboard-audit-output', $keyboard -Wait -PassThru
if ($keyboardProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe keyboard audit exited $($keyboardProcess.ExitCode)" }
python $sourceVerifier --bind-artifact $keyboard --bound-output $boundKeyboardAudit --digest-output $keyboardDigestPath
if ($LASTEXITCODE -ne 0) { throw "Keyboard evidence binding exited $LASTEXITCODE" }
$keyboardSha256 = (Get-Content -LiteralPath $keyboardDigestPath -Raw).Trim()
$keyboardEvidence = Get-Content $boundKeyboardAudit -Raw | ConvertFrom-Json
if ($keyboardEvidence.status -ne 'PASS') { throw 'Packaged keyboard audit did not PASS' }
if ($keyboardEvidence.human_tested -ne $false -or $keyboardEvidence.nvda_verified -ne $false) {
  throw 'Machine keyboard audit must not claim physical human/NVDA verification'
}

$restartRecovery = Join-Path $PWD 'dist/restart-recovery-audit.json'
if (Test-Path $restartRecovery) { Remove-Item -Force $restartRecovery }
$restartRecoveryProcess = Start-Process -FilePath $boundAutosportExe -ArgumentList '--restart-recovery-audit-output', $restartRecovery -Wait -PassThru
if ($restartRecoveryProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe restart/recovery audit exited $($restartRecoveryProcess.ExitCode)" }
python $sourceVerifier --bind-artifact $restartRecovery --bound-output $boundRestartRecoveryAudit --digest-output $restartRecoveryDigestPath
if ($LASTEXITCODE -ne 0) { throw "Restart/recovery evidence binding exited $LASTEXITCODE" }
$restartRecoverySha256 = (Get-Content -LiteralPath $restartRecoveryDigestPath -Raw).Trim()
$restartRecoveryEvidence = Get-Content $boundRestartRecoveryAudit -Raw | ConvertFrom-Json
if ($restartRecoveryEvidence.status -ne 'PASS') { throw 'Packaged restart/recovery audit did not PASS' }
if ($restartRecoveryEvidence.session_restart_status -ne 'PASS') { throw 'Packaged restart audit did not prove persistent session reopen' }
if ($restartRecoveryEvidence.transaction_recovery_status -ne 'PASS') { throw 'Packaged recovery audit did not prove transaction recovery' }
if ($restartRecoveryEvidence.recovery_disposition -ne 'aborted_uncommitted') { throw 'Packaged recovery audit disposition is not fail-closed' }
if ($restartRecoveryEvidence.real_money_execution -ne $false -or $restartRecoveryEvidence.human_tested -ne $false -or $restartRecoveryEvidence.nvda_verified -ne $false) {
  throw 'Machine restart/recovery audit violated release truth labels'
}
Assert-ProcessRecoveryEvidence -Evidence $restartRecoveryEvidence -Label 'Packaged restart/recovery audit'

$dataExe = $boundDataExe
if (-not (Test-Path $dataExe -PathType Leaf)) { throw 'Packaged build is missing Autosport-Data.exe' }
& $dataExe --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe help exited $LASTEXITCODE" }
& $dataExe compare-strategies --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe compare-strategies --help exited $LASTEXITCODE" }
& $dataExe walk-forward-evaluate --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe walk-forward-evaluate --help exited $LASTEXITCODE" }
& $dataExe acquire --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe acquire --help exited $LASTEXITCODE" }
& $dataExe build-corpus --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe build-corpus --help exited $LASTEXITCODE" }
& $dataExe build-corpus-from-bundle --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe build-corpus-from-bundle --help exited $LASTEXITCODE" }
& $dataExe import-betfair-historical --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe import-betfair-historical --help exited $LASTEXITCODE" }
& $dataExe verify-dataset examples/tt_demo | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe verify-dataset exited $LASTEXITCODE" }

# Execute the frozen evaluator, not only its help surface. This deterministic
# schema-v1 smoke fixture is sample evidence only and must never be promoted to
# real historical/OOS proof.
$walkForwardBundlePath = Join-Path $PWD 'dist/walk-forward-smoke-bundle.json'
$walkForwardReport = Join-Path $PWD 'dist/walk-forward-smoke-report.json'
$walkForwardBundle = [ordered]@{
  schema_version = 1
  bins = 5
  forecasts = @(
    [ordered]@{
      forecast_id = 'f-1'
      quote_key = 'm1|winner|a'
      probability = '0.70'
      model_id = 'model'
      model_version = '1'
      strategy_version = 'research-v1'
      model_training_cutoff_ts = '2026-01-01T00:00:00+00:00'
      input_cutoff_ts = '2026-02-01T12:00:00+00:00'
      generated_at = '2026-02-01T12:00:00+00:00'
      uncertainty = '0.10'
      evidence_hashes = @()
      market_snapshot_hash = ('a' * 64)
      provenance = [ordered]@{ source = 'packaged-smoke-fixture' }
    },
    [ordered]@{
      forecast_id = 'f-2'
      quote_key = 'm2|winner|b'
      probability = '0.30'
      model_id = 'model'
      model_version = '2'
      strategy_version = 'research-v2'
      model_training_cutoff_ts = '2026-02-10T00:00:00+00:00'
      input_cutoff_ts = '2026-03-01T12:00:00+00:00'
      generated_at = '2026-03-01T12:00:00+00:00'
      uncertainty = '0.20'
      evidence_hashes = @()
      market_snapshot_hash = ('b' * 64)
      provenance = [ordered]@{ source = 'packaged-smoke-fixture' }
    }
  )
  outcomes = @(
    [ordered]@{ forecast_id = 'f-1'; outcome = 1; revealed_at = '2026-02-02T12:00:00+00:00' },
    [ordered]@{ forecast_id = 'f-2'; outcome = 0; revealed_at = '2026-03-02T12:00:00+00:00' }
  )
  windows = @(
    [ordered]@{
      window_id = 'holdout-1'
      training_end_ts = '2026-01-31T23:59:59+00:00'
      evaluation_start_ts = '2026-02-01T00:00:00+00:00'
      evaluation_end_ts = '2026-02-28T23:59:59+00:00'
      split = 'holdout'
    },
    [ordered]@{
      window_id = 'holdout-2'
      training_end_ts = '2026-02-28T23:59:59+00:00'
      evaluation_start_ts = '2026-03-01T00:00:00+00:00'
      evaluation_end_ts = '2026-03-31T23:59:59+00:00'
      split = 'holdout'
    }
  )
}
$walkForwardJson = $walkForwardBundle | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($walkForwardBundlePath, $walkForwardJson, [System.Text.UTF8Encoding]::new($false))
if (Test-Path $walkForwardReport) { Remove-Item -Force $walkForwardReport }
& $dataExe walk-forward-evaluate $walkForwardBundlePath --output $walkForwardReport | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe walk-forward-evaluate exited $LASTEXITCODE" }
$walkForwardEvidence = Get-Content $walkForwardReport -Raw | ConvertFrom-Json
if ($walkForwardEvidence.kind -ne 'strict_walk_forward_forecast_evaluation') { throw 'Packaged walk-forward report kind mismatch' }
if ($walkForwardEvidence.evaluated_forecast_count -ne 2) { throw 'Packaged walk-forward report forecast count mismatch' }
if ($walkForwardEvidence.window_count -ne 2) { throw 'Packaged walk-forward report window count mismatch' }
if ([string]::IsNullOrWhiteSpace($walkForwardEvidence.source_sha256) -or $walkForwardEvidence.source_sha256.Length -ne 64) { throw 'Packaged walk-forward report source_sha256 is invalid' }
if ($walkForwardEvidence.profitability_claim -ne $false) { throw 'Packaged walk-forward smoke must not claim profitability' }
if ($walkForwardEvidence.real_money_execution -ne $false) { throw 'Packaged walk-forward smoke must preserve REAL_MONEY_EXECUTION=false' }

$package = Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'
$packageVerification = Join-Path $PWD 'dist/package-verification.json'
if (Test-Path $package) { Remove-Item -Force $package }
if (Test-Path $packageVerification) { Remove-Item -Force $packageVerification }
python $sourceVerifier --source-sha $sourceSha --late-build-boundary --allow-release-outputs
if ($LASTEXITCODE -ne 0) { throw "Trusted source gate before package assembly exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundAutosportExe --expected-sha256 $autosportExeSha256
if ($LASTEXITCODE -ne 0) { throw "Bound Autosport.exe verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundDataExe --expected-sha256 $dataExeSha256
if ($LASTEXITCODE -ne 0) { throw "Bound Autosport-Data.exe verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundDiagnostic --expected-sha256 $diagnosticSha256
if ($LASTEXITCODE -ne 0) { throw "Bound diagnostic evidence verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundAccessibilityAudit --expected-sha256 $accessibilitySha256
if ($LASTEXITCODE -ne 0) { throw "Bound accessibility evidence verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundKeyboardAudit --expected-sha256 $keyboardSha256
if ($LASTEXITCODE -ne 0) { throw "Bound keyboard evidence verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundRestartRecoveryAudit --expected-sha256 $restartRecoverySha256
if ($LASTEXITCODE -ne 0) { throw "Bound restart/recovery evidence verification exited $LASTEXITCODE" }
python scripts/package_windows.py `
  --exe $boundAutosportExe `
  --exe-sha256 $autosportExeSha256 `
  --data-exe $boundDataExe `
  --data-exe-sha256 $dataExeSha256 `
  --start-file WINDOWS_START_HERE.txt `
  --example-dir examples/tt_demo `
  --diagnostic $boundDiagnostic `
  --diagnostic-sha256 $diagnosticSha256 `
  --accessibility-audit $boundAccessibilityAudit `
  --accessibility-audit-sha256 $accessibilitySha256 `
  --keyboard-audit $boundKeyboardAudit `
  --keyboard-audit-sha256 $keyboardSha256 `
  --restart-recovery-audit $boundRestartRecoveryAudit `
  --restart-recovery-audit-sha256 $restartRecoverySha256 `
  --output $package `
  --source-sha $sourceSha `
  --verification-output $packageVerification
if ($LASTEXITCODE -ne 0) { throw "Windows package assembly exited $LASTEXITCODE" }

# Binding release gate: verify the artifact after a clean extraction, not only the
# pre-package executables. This catches archive/path/packaging defects that a
# successful dist smoke test cannot prove away.
$extractRoot = Join-Path $PWD '.build-fresh-extraction'
if (Test-Path $extractRoot) { Remove-Item -Recurse -Force $extractRoot }
New-Item -ItemType Directory -Path $extractRoot | Out-Null
Expand-Archive -LiteralPath $package -DestinationPath $extractRoot -Force
$packageRoot = Join-Path $extractRoot 'Autosport-V1'
$extractedExe = Join-Path $packageRoot 'Autosport.exe'
$extractedDataExe = Join-Path $packageRoot 'Autosport-Data.exe'
if (-not (Test-Path $extractedExe -PathType Leaf)) { throw 'Fresh extraction is missing Autosport.exe' }
if (-not (Test-Path $extractedDataExe -PathType Leaf)) { throw 'Fresh extraction is missing Autosport-Data.exe' }

$buildInfo = Get-Content (Join-Path $packageRoot 'BUILD_INFO.json') -Raw | ConvertFrom-Json
if ($buildInfo.source_sha -ne $sourceSha) { throw 'Fresh extraction BUILD_INFO source_sha mismatch' }
if ($buildInfo.real_money_execution -ne $false) { throw 'Fresh extraction must preserve REAL_MONEY_EXECUTION=false' }
if ($buildInfo.human_tested -ne $false) { throw 'Machine build must not claim HUMAN_TESTED' }
if ($buildInfo.nvda_verified -ne $false) { throw 'Machine build must not claim NVDA_VERIFIED' }
if ($buildInfo.portable_historical_data_tools -ne $true) { throw 'Fresh extraction does not bind portable historical data tools' }
$extractedExeSha = (Get-FileHash -LiteralPath $extractedExe -Algorithm SHA256).Hash.ToLowerInvariant()
$extractedDataExeSha = (Get-FileHash -LiteralPath $extractedDataExe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($extractedExeSha -ne $buildInfo.autosport_exe_sha256) { throw 'Fresh extraction Autosport.exe hash mismatch' }
if ($extractedDataExeSha -ne $buildInfo.autosport_data_exe_sha256) { throw 'Fresh extraction Autosport-Data.exe hash mismatch' }

& $extractedDataExe --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe help exited $LASTEXITCODE" }
& $extractedDataExe compare-strategies --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe compare-strategies --help exited $LASTEXITCODE" }
& $extractedDataExe walk-forward-evaluate --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe walk-forward-evaluate --help exited $LASTEXITCODE" }
& $extractedDataExe acquire --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe acquire --help exited $LASTEXITCODE" }
& $extractedDataExe build-corpus --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe build-corpus --help exited $LASTEXITCODE" }
& $extractedDataExe build-corpus-from-bundle --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe build-corpus-from-bundle --help exited $LASTEXITCODE" }
& $extractedDataExe import-betfair-historical --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe import-betfair-historical --help exited $LASTEXITCODE" }
& $extractedDataExe verify-dataset (Join-Path $packageRoot 'examples/tt_demo') | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe verify-dataset exited $LASTEXITCODE" }

$freshWalkForwardReport = Join-Path $PWD 'dist/fresh-extraction-walk-forward-report.json'
if (Test-Path $freshWalkForwardReport) { Remove-Item -Force $freshWalkForwardReport }
& $extractedDataExe walk-forward-evaluate $walkForwardBundlePath --output $freshWalkForwardReport | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe walk-forward-evaluate exited $LASTEXITCODE" }
$freshWalkForwardEvidence = Get-Content $freshWalkForwardReport -Raw | ConvertFrom-Json
if ($freshWalkForwardEvidence.kind -ne 'strict_walk_forward_forecast_evaluation') { throw 'Fresh-extracted walk-forward report kind mismatch' }
if ($freshWalkForwardEvidence.evaluated_forecast_count -ne 2) { throw 'Fresh-extracted walk-forward report forecast count mismatch' }
if ($freshWalkForwardEvidence.window_count -ne 2) { throw 'Fresh-extracted walk-forward report window count mismatch' }
if ($freshWalkForwardEvidence.source_sha256 -ne $walkForwardEvidence.source_sha256) { throw 'Fresh-extracted walk-forward report source identity mismatch' }
if ($freshWalkForwardEvidence.profitability_claim -ne $false) { throw 'Fresh-extracted walk-forward smoke must not claim profitability' }
if ($freshWalkForwardEvidence.real_money_execution -ne $false) { throw 'Fresh-extracted walk-forward smoke must preserve REAL_MONEY_EXECUTION=false' }

$freshDiag = Join-Path $PWD 'dist/fresh-extraction-diagnostic.json'
if (Test-Path $freshDiag) { Remove-Item -Force $freshDiag }
$freshDiagProcess = Start-Process -FilePath $extractedExe -ArgumentList '--diagnostic-output', $freshDiag -Wait -PassThru
if ($freshDiagProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe diagnostic exited $($freshDiagProcess.ExitCode)" }
$freshDiagnostic = Get-Content $freshDiag -Raw | ConvertFrom-Json
if ($freshDiagnostic.status -ne 'PASS') { throw 'Fresh-extracted Autosport.exe diagnostic did not PASS' }
if ($freshDiagnostic.real_money_execution -ne $false -or $freshDiagnostic.human_tested -ne $false -or $freshDiagnostic.nvda_verified -ne $false) {
  throw 'Fresh-extracted diagnostic violated release truth labels'
}

$freshA11y = Join-Path $PWD 'dist/fresh-extraction-accessibility-audit.json'
if (Test-Path $freshA11y) { Remove-Item -Force $freshA11y }
$freshA11yProcess = Start-Process -FilePath $extractedExe -ArgumentList '--accessibility-audit-output', $freshA11y -Wait -PassThru
if ($freshA11yProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe accessibility audit exited $($freshA11yProcess.ExitCode)" }
$freshAccessibility = Get-Content $freshA11y -Raw | ConvertFrom-Json
if ($freshAccessibility.status -ne 'PASS') { throw 'Fresh-extracted accessibility audit did not PASS' }
if ($freshAccessibility.real_money_execution -ne $false -or $freshAccessibility.human_tested -ne $false -or $freshAccessibility.nvda_verified -ne $false) {
  throw 'Fresh-extracted accessibility audit violated release truth labels'
}

$freshKeyboard = Join-Path $PWD 'dist/fresh-extraction-keyboard-audit.json'
if (Test-Path $freshKeyboard) { Remove-Item -Force $freshKeyboard }
$freshKeyboardProcess = Start-Process -FilePath $extractedExe -ArgumentList '--keyboard-audit-output', $freshKeyboard -Wait -PassThru
if ($freshKeyboardProcess.ExitCode -ne 0) { throw "Fresh-extracted keyboard audit exited $($freshKeyboardProcess.ExitCode)" }
$freshKeyboardEvidence = Get-Content $freshKeyboard -Raw | ConvertFrom-Json
if ($freshKeyboardEvidence.status -ne 'PASS') { throw 'Fresh-extracted keyboard audit did not PASS' }
if ($freshKeyboardEvidence.real_money_execution -ne $false -or $freshKeyboardEvidence.human_tested -ne $false -or $freshKeyboardEvidence.nvda_verified -ne $false) {
  throw 'Fresh-extracted keyboard audit violated release truth labels'
}

$freshRestartRecovery = Join-Path $PWD 'dist/fresh-extraction-restart-recovery-audit.json'
if (Test-Path $freshRestartRecovery) { Remove-Item -Force $freshRestartRecovery }
$freshRestartRecoveryProcess = Start-Process -FilePath $extractedExe -ArgumentList '--restart-recovery-audit-output', $freshRestartRecovery -Wait -PassThru
if ($freshRestartRecoveryProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe restart/recovery audit exited $($freshRestartRecoveryProcess.ExitCode)" }
$freshRestartRecoveryEvidence = Get-Content $freshRestartRecovery -Raw | ConvertFrom-Json
if ($freshRestartRecoveryEvidence.status -ne 'PASS') { throw 'Fresh-extracted restart/recovery audit did not PASS' }
if ($freshRestartRecoveryEvidence.session_restart_status -ne 'PASS') { throw 'Fresh-extracted restart audit did not prove persistent session reopen' }
if ($freshRestartRecoveryEvidence.transaction_recovery_status -ne 'PASS') { throw 'Fresh-extracted recovery audit did not prove transaction recovery' }
if ($freshRestartRecoveryEvidence.recovery_disposition -ne 'aborted_uncommitted') { throw 'Fresh-extracted recovery audit disposition is not fail-closed' }
if ($freshRestartRecoveryEvidence.real_money_execution -ne $false -or $freshRestartRecoveryEvidence.human_tested -ne $false -or $freshRestartRecoveryEvidence.nvda_verified -ne $false) {
  throw 'Machine restart/recovery audit violated release truth labels'
}
Assert-ProcessRecoveryEvidence -Evidence $freshRestartRecoveryEvidence -Label 'Fresh-extracted restart/recovery audit'

$freshEvidence = [ordered]@{
  status = 'PASS'
  source_sha = $sourceSha
  package_sha256 = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
  autosport_exe_sha256 = $extractedExeSha
  autosport_data_exe_sha256 = $extractedDataExeSha
  portable_historical_data_tools = $true
  package_verification_status = 'PASS'
  extracted_strategy_comparison_entry_status = 'PASS'
  extracted_walk_forward_evaluation_entry_status = 'PASS'
  extracted_walk_forward_evaluation_execution_status = 'PASS'
  extracted_walk_forward_sample_real_historical_proof = $false
  extracted_data_tool_help_status = 'PASS'
  extracted_data_tool_acquire_help_status = 'PASS'
  extracted_data_tool_build_corpus_help_status = 'PASS'
  extracted_data_tool_bundle_corpus_help_status = 'PASS'
  extracted_data_tool_verify_dataset_status = 'PASS'
  extracted_diagnostic_status = $freshDiagnostic.status
  extracted_accessibility_status = $freshAccessibility.status
  extracted_keyboard_status = $freshKeyboardEvidence.status
  extracted_restart_recovery_status = $freshRestartRecoveryEvidence.status
  extracted_session_restart_status = $freshRestartRecoveryEvidence.session_restart_status
  extracted_transaction_recovery_status = $freshRestartRecoveryEvidence.transaction_recovery_status
  extracted_process_kill_relaunch_status = $freshRestartRecoveryEvidence.process_kill_relaunch_status
  extracted_process_recovery_disposition = $freshRestartRecoveryEvidence.process_recovery_disposition
  extracted_process_recovery_registry_status = $freshRestartRecoveryEvidence.process_recovery_registry_status
  extracted_process_recovery_manifest_phase = $freshRestartRecoveryEvidence.process_recovery_manifest_phase
  real_money_execution = $false
  human_tested = $false
  nvda_verified = $false
}
$freshEvidence | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $PWD 'dist/fresh-extraction-verification.json') -Encoding utf8
Remove-Item -LiteralPath $sourceVerifier -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $boundArtifactRoot -Recurse -Force -ErrorAction SilentlyContinue