$ErrorActionPreference = 'Stop'

$bundlePath = Join-Path $PWD 'dist/walk-forward-package-smoke.json'
$datasetRoot = Join-Path $PWD 'dist/walk-forward-package-smoke-dataset'
$preReportPath = Join-Path $PWD 'dist/walk-forward-package-smoke-report.json'
$freshReportPath = Join-Path $PWD 'dist/fresh-extraction-walk-forward-package-smoke-report.json'

if (Test-Path $datasetRoot) { Remove-Item -Recurse -Force $datasetRoot }
New-Item -ItemType Directory -Path $datasetRoot | Out-Null

# Build a deterministic schema-v2 governed fixture using only Python stdlib.
# Python creates fixture bytes/identities; the frozen Autosport-Data.exe remains
# the evaluator under test. This fixture is machine evidence only, never real
# historical/OOS/licensing proof.
$fixtureBuilder = @'
import hashlib
import json
import sys
from pathlib import Path

bundle_path = Path(sys.argv[1])
dataset_root = Path(sys.argv[2])
dataset_root.mkdir(parents=True, exist_ok=True)
market_path = dataset_root / "market.jsonl"
results_path = dataset_root / "results.json"

market_events = [
    {
        "event_id": "m1",
        "market_id": "winner",
        "selection_id": "a",
        "decimal_odds": "1.80",
        "observed_ts": "2026-02-01T12:00:00+00:00",
        "source_id": "package-smoke-feed",
        "sequence": 1,
        "market_type": "winner",
        "source_ts": "2026-02-01T11:59:59+00:00",
        "ingest_ts": "2026-02-01T12:00:00+00:00",
        "metadata": {},
    },
    {
        "event_id": "m2",
        "market_id": "winner",
        "selection_id": "b",
        "decimal_odds": "2.10",
        "observed_ts": "2026-03-01T12:00:00+00:00",
        "source_id": "package-smoke-feed",
        "sequence": 2,
        "market_type": "winner",
        "source_ts": "2026-03-01T11:59:59+00:00",
        "ingest_ts": "2026-03-01T12:00:00+00:00",
        "metadata": {},
    },
]
market_bytes = "".join(
    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    for item in market_events
).encode("utf-8")
market_path.write_bytes(market_bytes)

reveal_after = "2026-04-01T00:00:00+00:00"
results = {
    "schema_version": 1,
    "outcome_reveal_after": reveal_after,
    "quote_outcomes": {
        "m1|winner|a": "win",
        "m2|winner|b": "loss",
    },
}
results_bytes = (
    json.dumps(results, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
).encode("utf-8")
results_path.write_bytes(results_bytes)

market_sha = hashlib.sha256(market_bytes).hexdigest()
results_sha = hashlib.sha256(results_bytes).hexdigest()
governance = {
    "source_identity": "package-smoke-feed:deterministic-governed-fixture",
    "terms_reference": "internal-machine-fixture-only",
    "retention_basis": "ephemeral CI fixture",
    "redistribution_policy": "internal_only",
    "acquired_at": "2026-04-02T00:00:00+00:00",
    "imported_at": "2026-04-02T00:01:00+00:00",
    "coverage": {
        "start_ts": "2026-02-01T00:00:00+00:00",
        "end_ts": "2026-03-31T23:59:59+00:00",
        "source_ids": ["package-smoke-feed"],
        "market_types": ["winner"],
    },
    "causality": {
        "strategy_time_field": "observed_ts",
        "outcome_reveal_after": reveal_after,
    },
}
manifest = {
    "schema_version": 2,
    "dataset_kind": "historical",
    "name": "governed Windows package smoke fixture",
    "sport": "table_tennis",
    "market_file": market_path.name,
    "results_file": results_path.name,
    "market_sha256": market_sha,
    "results_sha256": results_sha,
    "governance": governance,
}
identity_payload = {
    "schema_version": 2,
    "name": manifest["name"],
    "sport": manifest["sport"],
    "market_file": manifest["market_file"],
    "results_file": manifest["results_file"],
    "market_sha256": market_sha,
    "results_sha256": results_sha,
    "governance": governance,
}
identity_canonical = json.dumps(
    identity_payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
import_identity = hashlib.sha256(identity_canonical).hexdigest()
manifest["import_identity"] = import_identity
(dataset_root / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)

bundle = {
    "schema_version": 2,
    "dataset": {
        "path": dataset_root.name,
        "historical_import_identity": import_identity,
        "market_sha256": market_sha,
        "sealed_results_sha256": results_sha,
    },
    "bins": 5,
    "forecasts": [
        {
            "forecast_id": "package-smoke-f1",
            "quote_key": "m1|winner|a",
            "probability": "0.70",
            "model_id": "package-smoke-model",
            "model_version": "1",
            "strategy_version": "package-smoke-v1",
            "model_training_cutoff_ts": "2026-01-01T00:00:00+00:00",
            "input_cutoff_ts": "2026-02-01T12:00:00+00:00",
            "generated_at": "2026-02-01T12:00:00+00:00",
            "uncertainty": "0.10",
            "evidence_hashes": [],
            "market_snapshot_hash": "a" * 64,
            "provenance": {"source": "deterministic-governed-package-smoke"},
        },
        {
            "forecast_id": "package-smoke-f2",
            "quote_key": "m2|winner|b",
            "probability": "0.30",
            "model_id": "package-smoke-model",
            "model_version": "2",
            "strategy_version": "package-smoke-v1",
            "model_training_cutoff_ts": "2026-02-10T00:00:00+00:00",
            "input_cutoff_ts": "2026-03-01T12:00:00+00:00",
            "generated_at": "2026-03-01T12:00:00+00:00",
            "uncertainty": "0.20",
            "evidence_hashes": [],
            "market_snapshot_hash": "b" * 64,
            "provenance": {"source": "deterministic-governed-package-smoke"},
        },
    ],
    "outcomes": [
        {
            "forecast_id": "package-smoke-f1",
            "outcome": 1,
            "revealed_at": reveal_after,
        },
        {
            "forecast_id": "package-smoke-f2",
            "outcome": 0,
            "revealed_at": reveal_after,
        },
    ],
    "windows": [
        {
            "window_id": "package-smoke-holdout-1",
            "training_end_ts": "2026-01-31T23:59:59+00:00",
            "evaluation_start_ts": "2026-02-01T00:00:00+00:00",
            "evaluation_end_ts": "2026-02-28T23:59:59+00:00",
            "split": "holdout",
        },
        {
            "window_id": "package-smoke-holdout-2",
            "training_end_ts": "2026-02-28T23:59:59+00:00",
            "evaluation_start_ts": "2026-03-01T00:00:00+00:00",
            "evaluation_end_ts": "2026-03-31T23:59:59+00:00",
            "split": "holdout",
        },
    ],
}
bundle_path.write_text(
    json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps({
    "historical_import_identity": import_identity,
    "market_sha256": market_sha,
    "sealed_results_sha256": results_sha,
}, sort_keys=True, separators=(",", ":")))
'@

$fixtureMetadataJson = $fixtureBuilder | python - $bundlePath $datasetRoot
if ($LASTEXITCODE -ne 0) { throw "Governed walk-forward fixture generation exited $LASTEXITCODE" }
$fixtureMetadata = $fixtureMetadataJson | ConvertFrom-Json
if ($fixtureMetadata.historical_import_identity -notmatch '^[0-9a-f]{64}$') { throw 'Fixture historical_import_identity is invalid' }
if ($fixtureMetadata.market_sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Fixture market_sha256 is invalid' }
if ($fixtureMetadata.sealed_results_sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Fixture sealed_results_sha256 is invalid' }

function Invoke-WalkForwardPackageSmoke {
  param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$Output,
    [Parameter(Mandatory = $true)][string]$Label
  )

  if (-not (Test-Path $Exe -PathType Leaf)) { throw "$Label Autosport-Data.exe is missing" }
  if (Test-Path $Output) { Remove-Item -Force $Output }
  & $Exe walk-forward-evaluate $bundlePath --output $Output | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "$Label governed walk-forward evaluation exited $LASTEXITCODE" }
  if (-not (Test-Path $Output -PathType Leaf)) { throw "$Label governed walk-forward evaluation did not create a report" }

  $report = Get-Content $Output -Raw | ConvertFrom-Json
  if ($report.kind -ne 'strict_walk_forward_forecast_evaluation') { throw "$Label walk-forward report kind mismatch" }
  if ($report.input_bundle_schema_version -ne 2) { throw "$Label did not execute schema-v2 governed evaluation" }
  if ($report.evaluation_mode -ne 'governed-historical-complete-cohort-causal-walk-forward') {
    throw "$Label walk-forward evaluation_mode is not governed"
  }
  if ($report.evaluated_forecast_count -ne 2 -or $report.window_count -ne 2) {
    throw "$Label walk-forward report cohort/window count mismatch"
  }
  if ($report.source_sha256 -notmatch '^[0-9a-f]{64}$') { throw "$Label walk-forward report source_sha256 is invalid" }
  if ($report.dataset.historical_import_identity -ne $fixtureMetadata.historical_import_identity) {
    throw "$Label historical_import_identity does not match the fixture"
  }
  if ($report.dataset.market_sha256 -ne $fixtureMetadata.market_sha256) {
    throw "$Label market_sha256 does not match the fixture"
  }
  if ($report.dataset.sealed_results_sha256 -ne $fixtureMetadata.sealed_results_sha256) {
    throw "$Label sealed_results_sha256 does not match the fixture"
  }

  $truth = $report.truth
  if ($null -eq $truth) { throw "$Label governed walk-forward report is missing truth evidence" }
  if ($truth.governed_historical_import -ne $true) { throw "$Label did not verify governed historical import identity" }
  if ($truth.sealed_dataset_identity_verified -ne $true) { throw "$Label did not verify sealed dataset identity" }
  if ($truth.sealed_outcomes_bound_to_forecasts -ne $true) { throw "$Label did not bind sealed outcomes to forecasts" }
  if ($truth.outcome_reveal_boundary_verified -ne $true) { throw "$Label did not verify outcome reveal boundary" }
  if ($truth.temporal_timestamp_constraints_verified -ne $true) { throw "$Label did not verify governed timestamp constraints" }
  if ($truth.temporal_holdout_protocol_verified -ne $false) {
    throw "$Label must not promote timestamp-valid fixture data to verified holdout protocol evidence"
  }
  if ($truth.historical_window_market_coverage_verified -ne $false -or $truth.licensing_retention_verified -ne $false) {
    throw "$Label fixture must not claim real historical coverage/licensing proof"
  }
  if ($truth.profitability_claim -ne $false -or $truth.predictive_superiority_claim -ne $false -or $truth.real_money_execution -ne $false) {
    throw "$Label governed walk-forward report violated nested no-overclaim truth"
  }
  if ($report.profitability_claim -ne $false -or $report.real_money_execution -ne $false) {
    throw "$Label governed walk-forward report violated top-level no-overclaim truth"
  }
  return $report
}

$preExe = [string]$env:AUTOSPORT_PACKAGED_DATA_EXE
if ([string]::IsNullOrWhiteSpace($preExe)) { throw 'AUTOSPORT_PACKAGED_DATA_EXE verified package extraction anchor is missing' }
$freshExe = Join-Path $PWD '.build-fresh-extraction/Autosport-V1/Autosport-Data.exe'
$preReport = Invoke-WalkForwardPackageSmoke -Exe $preExe -Output $preReportPath -Label 'Packaged'
$freshReport = Invoke-WalkForwardPackageSmoke -Exe $freshExe -Output $freshReportPath -Label 'Fresh-extracted'

if ($freshReport.source_sha256 -ne $preReport.source_sha256) {
  throw 'Governed walk-forward bundle identity changed between packaged and fresh-extracted execution'
}
if ($freshReport.dataset.historical_import_identity -ne $preReport.dataset.historical_import_identity) {
  throw 'Governed historical import identity changed after fresh extraction'
}
if ($freshReport.dataset.market_sha256 -ne $preReport.dataset.market_sha256 -or $freshReport.dataset.sealed_results_sha256 -ne $preReport.dataset.sealed_results_sha256) {
  throw 'Governed dataset hashes changed after fresh extraction'
}
if ($freshReport.evaluated_forecast_count -ne $preReport.evaluated_forecast_count -or $freshReport.window_count -ne $preReport.window_count) {
  throw 'Governed walk-forward evaluation changed after fresh extraction'
}

$verificationPath = Join-Path $PWD 'dist/fresh-extraction-verification.json'
if (-not (Test-Path $verificationPath -PathType Leaf)) { throw 'Fresh-extraction verification evidence is missing' }
$verification = Get-Content $verificationPath -Raw | ConvertFrom-Json
$verification | Add-Member -NotePropertyName extracted_walk_forward_evaluation_execution_status -NotePropertyValue 'PASS' -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_schema_version -NotePropertyValue 2 -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_governed_fixture -NotePropertyValue $true -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_historical_import_identity -NotePropertyValue $fixtureMetadata.historical_import_identity -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_market_sha256 -NotePropertyValue $fixtureMetadata.market_sha256 -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_sealed_results_sha256 -NotePropertyValue $fixtureMetadata.sealed_results_sha256 -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_timestamp_constraints_verified -NotePropertyValue $true -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_temporal_holdout_protocol_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_sample_only -NotePropertyValue $true -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_real_historical_market_proof -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_historical_window_market_coverage_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_licensing_retention_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName walk_forward_smoke_profitability_claim -NotePropertyValue $false -Force
$verification | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $verificationPath -Encoding utf8
