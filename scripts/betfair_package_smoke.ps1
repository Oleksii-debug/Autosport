$ErrorActionPreference = 'Stop'

$packagedDataExe = Join-Path $PWD 'dist/Autosport-Data.exe'
$freshDataExe = Join-Path $PWD '.build-fresh-extraction/Autosport-V1/Autosport-Data.exe'
if (-not (Test-Path $packagedDataExe -PathType Leaf)) { throw 'Betfair packaged smoke is missing dist/Autosport-Data.exe' }
if (-not (Test-Path $freshDataExe -PathType Leaf)) { throw 'Betfair fresh-extraction smoke is missing Autosport-Data.exe' }

$sourcePath = Join-Path $PWD 'dist/betfair-package-smoke-source.jsonl'
$open = [ordered]@{
  op = 'mcm'
  pt = 1770724800000
  mc = @(
    [ordered]@{
      id = '1.100'
      marketDefinition = [ordered]@{
        eventId = 'event-ci-betfair-smoke'
        eventTypeId = '2593174'
        marketType = 'MATCH_ODDS'
        status = 'OPEN'
        eventName = 'Synthetic CI table tennis match'
        runners = @([ordered]@{ id = 101; name = 'Player A'; status = 'ACTIVE' })
      }
      rc = @([ordered]@{ id = 101; ltp = 1.8 })
    }
  )
}
$closed = [ordered]@{
  op = 'mcm'
  pt = 1770728400000
  mc = @(
    [ordered]@{
      id = '1.100'
      marketDefinition = [ordered]@{
        eventId = 'event-ci-betfair-smoke'
        eventTypeId = '2593174'
        marketType = 'MATCH_ODDS'
        status = 'CLOSED'
        eventName = 'Synthetic CI table tennis match'
        runners = @([ordered]@{ id = 101; name = 'Player A'; status = 'WINNER' })
      }
    }
  )
}
$lines = @(
  ($open | ConvertTo-Json -Depth 8 -Compress),
  ($closed | ConvertTo-Json -Depth 8 -Compress)
)
[System.IO.File]::WriteAllLines($sourcePath, $lines, [System.Text.UTF8Encoding]::new($false))

function Invoke-BetfairFrozenImportSmoke {
  param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [Parameter(Mandatory=$true)][string]$Label
  )

  if (Test-Path $OutputDir) { Remove-Item -Recurse -Force $OutputDir }
  $importOutput = (& $Executable import-betfair-historical $sourcePath `
    --output-dir $OutputDir `
    --acquired-at '2026-02-10T13:30:00Z' `
    --terms-reference 'synthetic-ci-fixture-not-license-proof' `
    --retention-basis 'repository-ci-fixture-only' `
    --redistribution-policy prohibited 2>&1 | Out-String).Trim()
  if ($LASTEXITCODE -ne 0) { throw "$Label frozen Betfair import exited $LASTEXITCODE`: $importOutput" }
  if ($importOutput -notmatch 'betfair_historical_import=IMPORTED') { throw "$Label frozen Betfair import did not report IMPORTED" }
  if ($importOutput -notmatch 'licensing_retention_verified=false') { throw "$Label frozen Betfair import violated licensing truth boundary" }
  if ($importOutput -notmatch 'real_money_execution=false') { throw "$Label frozen Betfair import violated REAL_MONEY_EXECUTION=false" }

  $verifyOutput = (& $Executable verify-dataset $OutputDir 2>&1 | Out-String).Trim()
  if ($LASTEXITCODE -ne 0) { throw "$Label frozen verify-dataset exited $LASTEXITCODE`: $verifyOutput" }

  $manifestPath = Join-Path $OutputDir 'manifest.json'
  $marketPath = Join-Path $OutputDir 'market.jsonl'
  if (-not (Test-Path $manifestPath -PathType Leaf)) { throw "$Label frozen import did not create manifest.json" }
  if (-not (Test-Path $marketPath -PathType Leaf)) { throw "$Label frozen import did not create market.jsonl" }
  $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
  if ($manifest.governance.price_semantics.decimal_odds -ne 'betfair_last_traded_price') { throw "$Label manifest lost Betfair LTP semantics" }
  if ($manifest.governance.price_semantics.provider_field -ne 'rc[].ltp') { throw "$Label manifest lost provider price field" }
  if ($manifest.governance.price_semantics.execution_quote_verified -ne $false) { throw "$Label manifest incorrectly claims executable quote verification" }
  if ($manifest.governance.availability_semantics.complete_availability_history_verified -ne $false) { throw "$Label manifest incorrectly claims complete availability history" }
  if ($manifest.governance.terms_reference -ne 'synthetic-ci-fixture-not-license-proof') { throw "$Label manifest terms reference drifted" }
  if ($manifest.governance.retention_basis -ne 'repository-ci-fixture-only') { throw "$Label manifest retention basis drifted" }

  $event = (Get-Content $marketPath | Select-Object -First 1) | ConvertFrom-Json
  if ($event.metadata.price_semantics -ne 'betfair_last_traded_price') { throw "$Label canonical MarketEvent lost LTP semantics" }
  if ($event.metadata.execution_quote_verified -ne $false) { throw "$Label canonical MarketEvent incorrectly claims executable quote verification" }

  return [ordered]@{
    status = 'PASS'
    import_identity = [string]$manifest.import_identity
    market_sha256 = [string]$manifest.market_sha256
    results_sha256 = [string]$manifest.results_sha256
    execution_quote_verified = $false
    complete_availability_history_verified = $false
  }
}

$packagedDataset = Join-Path $PWD 'dist/betfair-package-smoke-packaged-dataset'
$freshDataset = Join-Path $PWD 'dist/betfair-package-smoke-fresh-dataset'
$packaged = Invoke-BetfairFrozenImportSmoke -Executable $packagedDataExe -OutputDir $packagedDataset -Label 'packaged'
$fresh = Invoke-BetfairFrozenImportSmoke -Executable $freshDataExe -OutputDir $freshDataset -Label 'fresh-extracted'

$evidence = [ordered]@{
  status = 'PASS'
  source_sha = $env:AUTOSPORT_SOURCE_SHA
  fixture_kind = 'synthetic_local_ci_fixture'
  packaged = $packaged
  fresh_extracted = $fresh
  frozen_importer_executed = $true
  canonical_verify_dataset_executed = $true
  ltp_semantics_bound = $true
  licensing_retention_verified = $false
  real_historical_market_proof = $false
  profitability_claim = $false
  real_money_execution = $false
  human_tested = $false
  nvda_verified = $false
}
$evidencePath = Join-Path $PWD 'dist/betfair-package-smoke.json'
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $evidencePath -Encoding utf8
