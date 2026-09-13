$ErrorActionPreference = 'Stop'
python -m pip install --upgrade pip
python -m pip install -e '.[build]'
python -m unittest discover -s tests -v
if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }
python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Autosport src/autosport/windows_entry.py

$diag = Join-Path $PWD 'dist/packaged-diagnostic.json'
if (Test-Path $diag) { Remove-Item -Force $diag }
$process = Start-Process -FilePath (Join-Path $PWD 'dist/Autosport.exe') -ArgumentList '--diagnostic-output', $diag -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Packaged Autosport.exe diagnostic exited $($process.ExitCode)" }
$diagnostic = Get-Content $diag -Raw | ConvertFrom-Json
if ($diagnostic.status -ne 'PASS') { throw 'Packaged Autosport.exe diagnostic did not PASS' }

$a11y = Join-Path $PWD 'dist/accessibility-audit.json'
if (Test-Path $a11y) { Remove-Item -Force $a11y }
$a11yProcess = Start-Process -FilePath (Join-Path $PWD 'dist/Autosport.exe') -ArgumentList '--accessibility-audit-output', $a11y -Wait -PassThru
if ($a11yProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe accessibility audit exited $($a11yProcess.ExitCode)" }
$accessibility = Get-Content $a11y -Raw | ConvertFrom-Json
if ($accessibility.status -ne 'PASS') { throw 'Packaged accessibility audit did not PASS' }
if ($accessibility.nvda_verified -ne $false) { throw 'Machine accessibility audit must not claim NVDA verification' }

$keyboard = Join-Path $PWD 'dist/keyboard-audit.json'
if (Test-Path $keyboard) { Remove-Item -Force $keyboard }
$keyboardProcess = Start-Process -FilePath (Join-Path $PWD 'dist/Autosport.exe') -ArgumentList '--keyboard-audit-output', $keyboard -Wait -PassThru
if ($keyboardProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe keyboard audit exited $($keyboardProcess.ExitCode)" }
$keyboardEvidence = Get-Content $keyboard -Raw | ConvertFrom-Json
if ($keyboardEvidence.status -ne 'PASS') { throw 'Packaged keyboard audit did not PASS' }
if ($keyboardEvidence.human_tested -ne $false -or $keyboardEvidence.nvda_verified -ne $false) {
  throw 'Machine keyboard audit must not claim physical human/NVDA verification'
}

$restartRecovery = Join-Path $PWD 'dist/restart-recovery-audit.json'
if (Test-Path $restartRecovery) { Remove-Item -Force $restartRecovery }
$restartRecoveryProcess = Start-Process -FilePath (Join-Path $PWD 'dist/Autosport.exe') -ArgumentList '--restart-recovery-audit-output', $restartRecovery -Wait -PassThru
if ($restartRecoveryProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe restart/recovery audit exited $($restartRecoveryProcess.ExitCode)" }
$restartRecoveryEvidence = Get-Content $restartRecovery -Raw | ConvertFrom-Json
if ($restartRecoveryEvidence.status -ne 'PASS') { throw 'Packaged restart/recovery audit did not PASS' }
if ($restartRecoveryEvidence.session_restart_status -ne 'PASS') { throw 'Packaged restart audit did not prove persistent session reopen' }
if ($restartRecoveryEvidence.transaction_recovery_status -ne 'PASS') { throw 'Packaged recovery audit did not prove transaction recovery' }
if ($restartRecoveryEvidence.recovery_disposition -ne 'aborted_uncommitted') { throw 'Packaged recovery audit disposition is not fail-closed' }
if ($restartRecoveryEvidence.real_money_execution -ne $false -or $restartRecoveryEvidence.human_tested -ne $false -or $restartRecoveryEvidence.nvda_verified -ne $false) {
  throw 'Machine restart/recovery audit violated release truth labels'
}

$productJourney = Join-Path $PWD 'dist/product-journey-audit.json'
if (Test-Path $productJourney) { Remove-Item -Force $productJourney }
$demoDataset = Join-Path $PWD 'examples/tt_demo'
$demoPlan = Join-Path $demoDataset 'research_plan.json'
$productJourneyProcess = Start-Process -FilePath (Join-Path $PWD 'dist/Autosport.exe') -ArgumentList '--product-journey-audit-output', $productJourney, '--dataset-root', $demoDataset, '--research-plan', $demoPlan -Wait -PassThru
if ($productJourneyProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe product journey audit exited $($productJourneyProcess.ExitCode)" }
$productJourneyEvidence = Get-Content $productJourney -Raw | ConvertFrom-Json
if ($productJourneyEvidence.status -ne 'PASS') { throw 'Packaged product journey audit did not PASS' }
if ($productJourneyEvidence.canonical_strategy_id -ne 'research-replay-v1') { throw 'Packaged product journey did not exercise research-replay-v1' }
if ($productJourneyEvidence.transaction_terminal -ne $true) { throw 'Packaged product journey did not reach terminal transaction state' }
if ($productJourneyEvidence.settled_ticket_count -ne 1) { throw 'Packaged product journey did not settle the deterministic demo ticket' }
if ($productJourneyEvidence.profitability_claim -ne $false -or $productJourneyEvidence.real_historical_point_in_time_market_coverage_verified -ne $false) {
  throw 'Packaged product journey violated sample/research truth labels'
}
if ($productJourneyEvidence.real_money_execution -ne $false -or $productJourneyEvidence.human_tested -ne $false -or $productJourneyEvidence.nvda_verified -ne $false) {
  throw 'Packaged product journey violated release truth labels'
}

$sourceSha = $env:AUTOSPORT_SOURCE_SHA
if ([string]::IsNullOrWhiteSpace($sourceSha)) { $sourceSha = (git rev-parse HEAD).Trim() }
$package = Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'
$packageVerification = Join-Path $PWD 'dist/package-verification.json'
python scripts/package_windows.py `
  --exe dist/Autosport.exe `
  --start-file WINDOWS_START_HERE.txt `
  --example-dir examples/tt_demo `
  --diagnostic $diag `
  --accessibility-audit $a11y `
  --keyboard-audit $keyboard `
  --restart-recovery-audit $restartRecovery `
  --product-journey-audit $productJourney `
  --output $package `
  --source-sha $sourceSha `
  --verification-output $packageVerification

# Binding release gate: verify the artifact after a clean extraction, not only the
# pre-package executable. This catches archive/path/packaging defects that a
# successful dist/Autosport.exe smoke test cannot prove away.
$extractRoot = Join-Path $PWD '.build-fresh-extraction'
if (Test-Path $extractRoot) { Remove-Item -Recurse -Force $extractRoot }
New-Item -ItemType Directory -Path $extractRoot | Out-Null
Expand-Archive -LiteralPath $package -DestinationPath $extractRoot -Force
$packageRoot = Join-Path $extractRoot 'Autosport-V1'
$extractedExe = Join-Path $packageRoot 'Autosport.exe'
if (-not (Test-Path $extractedExe -PathType Leaf)) { throw 'Fresh extraction is missing Autosport.exe' }

$buildInfo = Get-Content (Join-Path $packageRoot 'BUILD_INFO.json') -Raw | ConvertFrom-Json
if ($buildInfo.source_sha -ne $sourceSha) { throw 'Fresh extraction BUILD_INFO source_sha mismatch' }
if ($buildInfo.real_money_execution -ne $false) { throw 'Fresh extraction must preserve REAL_MONEY_EXECUTION=false' }
if ($buildInfo.human_tested -ne $false) { throw 'Machine build must not claim HUMAN_TESTED' }
if ($buildInfo.nvda_verified -ne $false) { throw 'Machine build must not claim NVDA_VERIFIED' }
$extractedExeSha = (Get-FileHash -LiteralPath $extractedExe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($extractedExeSha -ne $buildInfo.autosport_exe_sha256) { throw 'Fresh extraction Autosport.exe hash mismatch' }

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
if ($freshKeyboardProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe keyboard audit exited $($freshKeyboardProcess.ExitCode)" }
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
  throw 'Fresh-extracted restart/recovery audit violated release truth labels'
}

$freshProductJourney = Join-Path $PWD 'dist/fresh-extraction-product-journey-audit.json'
if (Test-Path $freshProductJourney) { Remove-Item -Force $freshProductJourney }
$extractedDataset = Join-Path $packageRoot 'examples/tt_demo'
$extractedPlan = Join-Path $extractedDataset 'research_plan.json'
$freshProductJourneyProcess = Start-Process -FilePath $extractedExe -ArgumentList '--product-journey-audit-output', $freshProductJourney, '--dataset-root', $extractedDataset, '--research-plan', $extractedPlan -Wait -PassThru
if ($freshProductJourneyProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe product journey audit exited $($freshProductJourneyProcess.ExitCode)" }
$freshProductJourneyEvidence = Get-Content $freshProductJourney -Raw | ConvertFrom-Json
if ($freshProductJourneyEvidence.status -ne 'PASS') { throw 'Fresh-extracted product journey audit did not PASS' }
if ($freshProductJourneyEvidence.canonical_strategy_id -ne 'research-replay-v1') { throw 'Fresh-extracted product journey did not exercise research-replay-v1' }
if ($freshProductJourneyEvidence.transaction_terminal -ne $true) { throw 'Fresh-extracted product journey did not reach terminal transaction state' }
if ($freshProductJourneyEvidence.settled_ticket_count -ne 1) { throw 'Fresh-extracted product journey did not settle the deterministic demo ticket' }
if ($freshProductJourneyEvidence.profitability_claim -ne $false -or $freshProductJourneyEvidence.real_historical_point_in_time_market_coverage_verified -ne $false) {
  throw 'Fresh-extracted product journey violated sample/research truth labels'
}
if ($freshProductJourneyEvidence.real_money_execution -ne $false -or $freshProductJourneyEvidence.human_tested -ne $false -or $freshProductJourneyEvidence.nvda_verified -ne $false) {
  throw 'Fresh-extracted product journey violated release truth labels'
}

$freshEvidence = [ordered]@{
  status = 'PASS'
  source_sha = $sourceSha
  package_sha256 = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
  autosport_exe_sha256 = $extractedExeSha
  package_verification_status = 'PASS'
  extracted_diagnostic_status = $freshDiagnostic.status
  extracted_accessibility_status = $freshAccessibility.status
  extracted_keyboard_status = $freshKeyboardEvidence.status
  extracted_restart_recovery_status = $freshRestartRecoveryEvidence.status
  extracted_session_restart_status = $freshRestartRecoveryEvidence.session_restart_status
  extracted_transaction_recovery_status = $freshRestartRecoveryEvidence.transaction_recovery_status
  extracted_product_journey_status = $freshProductJourneyEvidence.status
  extracted_product_journey_strategy = $freshProductJourneyEvidence.canonical_strategy_id
  real_historical_point_in_time_market_coverage_verified = $false
  profitability_claim = $false
  real_money_execution = $false
  human_tested = $false
  nvda_verified = $false
}
$freshEvidence | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $PWD 'dist/fresh-extraction-verification.json') -Encoding utf8
