$ErrorActionPreference = 'Stop'
python -m pip install --upgrade pip
python -m pip install -e '.[build]'
python -m unittest discover -s tests -v
if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }
python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Autosport src/autosport/windows_entry.py
python -m PyInstaller --noconfirm --clean --onefile --console --name Autosport-Data src/autosport/data_tools_entry.py

$strategyComparisonProcess = Start-Process -FilePath (Join-Path $PWD 'dist/Autosport.exe') -ArgumentList 'compare-strategies', '--help' -Wait -PassThru
if ($strategyComparisonProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe strategy comparison entry exited $($strategyComparisonProcess.ExitCode)" }

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

$dataExe = Join-Path $PWD 'dist/Autosport-Data.exe'
if (-not (Test-Path $dataExe -PathType Leaf)) { throw 'Packaged build is missing Autosport-Data.exe' }
& $dataExe --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe help exited $LASTEXITCODE" }
& $dataExe acquire --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe acquire --help exited $LASTEXITCODE" }
& $dataExe build-corpus --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe build-corpus --help exited $LASTEXITCODE" }
& $dataExe build-corpus-from-bundle --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe build-corpus-from-bundle --help exited $LASTEXITCODE" }
& $dataExe verify-dataset examples/tt_demo | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe verify-dataset exited $LASTEXITCODE" }

$sourceSha = $env:AUTOSPORT_SOURCE_SHA
if ([string]::IsNullOrWhiteSpace($sourceSha)) { $sourceSha = (git rev-parse HEAD).Trim() }
$package = Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'
$packageVerification = Join-Path $PWD 'dist/package-verification.json'
python scripts/package_windows.py `
  --exe dist/Autosport.exe `
  --data-exe dist/Autosport-Data.exe `
  --start-file WINDOWS_START_HERE.txt `
  --example-dir examples/tt_demo `
  --diagnostic $diag `
  --accessibility-audit $a11y `
  --keyboard-audit $keyboard `
  --restart-recovery-audit $restartRecovery `
  --output $package `
  --source-sha $sourceSha `
  --verification-output $packageVerification

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

$freshStrategyComparisonProcess = Start-Process -FilePath $extractedExe -ArgumentList 'compare-strategies', '--help' -Wait -PassThru
if ($freshStrategyComparisonProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe strategy comparison entry exited $($freshStrategyComparisonProcess.ExitCode)" }

& $extractedDataExe --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe help exited $LASTEXITCODE" }
& $extractedDataExe acquire --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe acquire --help exited $LASTEXITCODE" }
& $extractedDataExe build-corpus --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe build-corpus --help exited $LASTEXITCODE" }
& $extractedDataExe build-corpus-from-bundle --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe build-corpus-from-bundle --help exited $LASTEXITCODE" }
& $extractedDataExe verify-dataset (Join-Path $packageRoot 'examples/tt_demo') | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe verify-dataset exited $LASTEXITCODE" }

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

$freshEvidence = [ordered]@{
  status = 'PASS'
  source_sha = $sourceSha
  package_sha256 = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
  autosport_exe_sha256 = $extractedExeSha
  autosport_data_exe_sha256 = $extractedDataExeSha
  portable_historical_data_tools = $true
  package_verification_status = 'PASS'
  extracted_strategy_comparison_entry_status = 'PASS'
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
  real_money_execution = $false
  human_tested = $false
  nvda_verified = $false
}
$freshEvidence | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $PWD 'dist/fresh-extraction-verification.json') -Encoding utf8