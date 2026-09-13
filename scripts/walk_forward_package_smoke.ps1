$ErrorActionPreference = 'Stop'

$bundlePath = Join-Path $PWD 'dist/walk-forward-package-smoke.json'
$preReportPath = Join-Path $PWD 'dist/walk-forward-package-smoke-report.json'
$freshReportPath = Join-Path $PWD 'dist/fresh-extraction-walk-forward-package-smoke-report.json'

$bundle = [ordered]@{
  schema_version = 1
  bins = 5
  forecasts = @(
    [ordered]@{
      forecast_id = 'package-smoke-f1'
      quote_key = 'sample|winner|a'
      probability = '0.70'
      model_id = 'package-smoke-model'
      model_version = '1'
      strategy_version = 'package-smoke-v1'
      model_training_cutoff_ts = '2026-01-01T00:00:00+00:00'
      input_cutoff_ts = '2026-02-01T12:00:00+00:00'
      generated_at = '2026-02-01T12:00:00+00:00'
      uncertainty = '0.10'
      evidence_hashes = @()
      market_snapshot_hash = ('a' * 64)
      provenance = [ordered]@{ source = 'deterministic-package-smoke-fixture' }
    },
    [ordered]@{
      forecast_id = 'package-smoke-f2'
      quote_key = 'sample|winner|b'
      probability = '0.30'
      model_id = 'package-smoke-model'
      model_version = '2'
      strategy_version = 'package-smoke-v1'
      model_training_cutoff_ts = '2026-02-10T00:00:00+00:00'
      input_cutoff_ts = '2026-03-01T12:00:00+00:00'
      generated_at = '2026-03-01T12:00:00+00:00'
      uncertainty = '0.20'
      evidence_hashes = @()
      market_snapshot_hash = ('b' * 64)
      provenance = [ordered]@{ source = 'deterministic-package-smoke-fixture' }
    }
  )
  outcomes = @(
    [ordered]@{ forecast_id = 'package-smoke-f1'; outcome = 1; revealed_at = '2026-02-02T12:00:00+00:00' },
    [ordered]@{ forecast_id = 'package-smoke-f2'; outcome = 0; revealed_at = '2026-03-02T12:00:00+00:00' }
  )
  windows = @(
    [ordered]@{
      window_id = 'package-smoke-holdout-1'
      training_end_ts = '2026-01-31T23:59:59+00:00'
      evaluation_start_ts = '2026-02-01T00:00:00+00:00'
      evaluation_end_ts = '2026-02-28T23:59:59+00:00'
      split = 'holdout'
    },
    [ordered]@{
      window_id = 'package-smoke-holdout-2'
      training_end_ts = '2026-02-28T23:59:59+00:00'
      evaluation_start_ts = '2026-03-01T00:00:00+00:00'
      evaluation_end_ts = '2026-03-31T23:59:59+00:00'
      split = 'holdout'
    }
  )
}
$bundle | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $bundlePath -Encoding utf8

function Invoke-WalkForwardPackageSmoke {
  param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$Output,
    [Parameter(Mandatory = $true)][string]$Label
  )

  if (-not (Test-Path $Exe -PathType Leaf)) { throw "$Label Autosport-Data.exe is missing" }
  if (Test-Path $Output) { Remove-Item -Force $Output }
  & $Exe walk-forward-evaluate $bundlePath --output $Output | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "$Label walk-forward evaluation exited $LASTEXITCODE" }
  if (-not (Test-Path $Output -PathType Leaf)) { throw "$Label walk-forward evaluation did not create a report" }

  $report = Get-Content $Output -Raw | ConvertFrom-Json
  if ($report.kind -ne 'strict_walk_forward_forecast_evaluation') { throw "$Label walk-forward report kind mismatch" }
  if ($report.evaluated_forecast_count -ne 2 -or $report.window_count -ne 2) {
    throw "$Label walk-forward report cohort/window count mismatch"
  }
  if ($report.source_sha256 -notmatch '^[0-9a-f]{64}$') { throw "$Label walk-forward report source_sha256 is invalid" }
  if ($report.profitability_claim -ne $false -or $report.real_money_execution -ne $false) {
    throw "$Label walk-forward report violated top-level no-overclaim truth"
  }
  if ($null -ne $report.truth) {
    if ($report.truth.profitability_claim -ne $false -or $report.truth.predictive_superiority_claim -ne $false -or $report.truth.real_money_execution -ne $false) {
      throw "$Label walk-forward report violated nested no-overclaim truth"
    }
    if ($report.truth.historical_window_market_coverage_verified -ne $false -or $report.truth.licensing_retention_verified -ne $false) {
      throw "$Label sample smoke report must not claim real historical coverage/licensing proof"
    }
  }
  return $report
}

$preExe = Join-Path $PWD 'dist/Autosport-Data.exe'
$freshExe = Join-Path $PWD '.build-fresh-extraction/Autosport-V1/Autosport-Data.exe'
$preReport = Invoke-WalkForwardPackageSmoke -Exe $preExe -Output $preReportPath -Label 'Packaged'
$freshReport = Invoke-WalkForwardPackageSmoke -Exe $freshExe -Output $freshReportPath -Label 'Fresh-extracted'

if ($freshReport.source_sha256 -ne $preReport.source_sha256) {
  throw 'Walk-forward smoke bundle identity changed between packaged and fresh-extracted execution'
}
if ($freshReport.evaluated_forecast_count -ne $preReport.evaluated_forecast_count -or $freshReport.window_count -ne $preReport.window_count) {
  throw 'Walk-forward smoke evaluation changed after fresh extraction'
}

$verificationPath = Join-Path $PWD 'dist/fresh-extraction-verification.json'
if (-not (Test-Path $verificationPath -PathType Leaf)) { throw 'Fresh-extraction verification evidence is missing' }
$verification = Get-Content $verificationPath -Raw | ConvertFrom-Json
$verification | Add-Member -NotePropertyName extracted_walk_forward_evaluation_execution_status -NotePropertyValue 'PASS' -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_sample_only -NotePropertyValue $true -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_real_historical_market_proof -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_profitability_claim -NotePropertyValue $false -Force
$verification | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $verificationPath -Encoding utf8
