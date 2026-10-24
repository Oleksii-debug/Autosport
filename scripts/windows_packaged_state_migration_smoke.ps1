param(
    [Parameter(Mandatory = $true)][string]$PackagedDataExe,
    [Parameter(Mandatory = $true)][string]$SourceSha,
    [Parameter(Mandatory = $true)][string]$EvidencePath
)

$ErrorActionPreference = 'Stop'
if ($SourceSha -notmatch '^[0-9a-f]{40}$') { throw 'State migration smoke requires canonical source SHA' }
if (-not (Test-Path -LiteralPath $PackagedDataExe -PathType Leaf)) { throw 'Packaged data executable is missing' }

$root = Join-Path $PWD 'dist/packaged-state-migration-smoke'
if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
New-Item -ItemType Directory -Path $root | Out-Null
$database = Join-Path $root 'стан з пробілами.db'

$fixture = @'
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore

path = Path(sys.argv[1])
event = MarketEvent(
    event_id="event-1", market_id="winner", selection_id="home",
    decimal_odds=Decimal("2.50"), observed_ts="2026-09-17T01:05:00Z",
    ingest_ts="2026-09-17T01:05:00Z", source_id="provider-a", sequence=7,
)
store = SQLiteMarketStore(path)
store.append(event)
store.close()
payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
with sqlite3.connect(path) as connection:
    connection.execute("DROP TABLE current_quotes")
    connection.execute("CREATE TABLE current_quotes (quote_key TEXT PRIMARY KEY, observed_ts TEXT NOT NULL, sequence INTEGER NOT NULL, payload_json TEXT NOT NULL)")
    connection.execute("INSERT INTO current_quotes (quote_key,observed_ts,sequence,payload_json) VALUES (?,?,?,?)", (event.quote_key, event.observed_ts, event.sequence, payload))
'@
& python -c $fixture $database
if ($LASTEXITCODE -ne 0) { throw "Legacy market-store fixture exited $LASTEXITCODE" }

$firstText = (& $PackagedDataExe migrate-market-store --database $database 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Packaged market-store migration exited $LASTEXITCODE" }
$first = $firstText | ConvertFrom-Json
if ($first.status -ne 'PASS' -or $first.migration_applied -ne $true) { throw 'Packaged executable did not apply the canonical legacy migration' }
if ($first.state_kind -cne 'canonical_market_store') { throw 'Packaged migration reported an unexpected state kind' }
if ($first.history_count -ne 1 -or $first.current_count -ne 1) { throw 'Packaged migration lost canonical market state' }
foreach ($field in @('before_sha256', 'after_sha256', 'state_sha256')) {
    if ([string]$first.$field -cnotmatch '^[0-9a-f]{64}$') { throw "Packaged migration emitted invalid $field" }
}

$secondText = (& $PackagedDataExe migrate-market-store --database $database 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Packaged market-store migration readback exited $LASTEXITCODE" }
$second = $secondText | ConvertFrom-Json
if ($second.status -ne 'PASS' -or $second.migration_applied -ne $false) { throw 'Packaged migration is not idempotent on reopen' }
if ($second.state_kind -cne 'canonical_market_store') { throw 'Packaged migration readback reported an unexpected state kind' }
if ($second.history_count -ne 1 -or $second.current_count -ne 1) { throw 'Packaged migration readback lost canonical market state' }
if ([string]$second.state_sha256 -ne [string]$first.state_sha256) {
    throw 'Packaged migration logical-state digest drifted'
}
foreach ($record in @($first, $second)) {
    foreach ($field in @('real_money_execution', 'human_tested', 'nvda_verified')) {
        if ($record.$field -ne $false) { throw "Packaged migration evidence $field is not false" }
    }
}

[ordered]@{
    status = 'PASS'
    evidence_class = 'AUTOMATED_WINDOWS_PACKAGED_STATE_MIGRATION'
    source_sha = $SourceSha
    data_exe_sha256 = (Get-FileHash -LiteralPath $PackagedDataExe -Algorithm SHA256).Hash.ToLowerInvariant()
    before_sha256 = [string]$first.before_sha256
    after_sha256 = [string]$first.after_sha256
    state_sha256 = [string]$first.state_sha256
    migration_applied = $true
    idempotent_reopen = $true
    history_count = 1
    current_count = 1
    schema_scope = 'canonical_market_store_legacy_current_quotes_projection'
    cross_package_schema_migration_qualified = $false
    real_money_execution = $false
    human_tested = $false
    nvda_verified = $false
    whole_product_complete = $false
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
