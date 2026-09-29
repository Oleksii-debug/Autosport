$ErrorActionPreference = 'Stop'

$sourceBundlePath = Join-Path $PWD 'dist/walk-forward-package-smoke.json'
$datasetRoot = Join-Path $PWD 'dist/walk-forward-package-smoke-dataset'
$bundlePath = Join-Path $PWD 'dist/walk-forward-origin-package-smoke.json'
$workspaceRoot = Join-Path $PWD 'dist/walk-forward-origin-package-smoke-workspace'
$preReportPath = Join-Path $PWD 'dist/walk-forward-origin-package-smoke-report.json'
$freshReportPath = Join-Path $PWD 'dist/fresh-extraction-walk-forward-origin-package-smoke-report.json'

if (-not (Test-Path $sourceBundlePath -PathType Leaf)) {
  throw 'Governed walk-forward package smoke bundle must exist before forecast-origin smoke'
}
if (-not (Test-Path (Join-Path $datasetRoot 'manifest.json') -PathType Leaf)) {
  throw 'Governed walk-forward package smoke dataset must exist before forecast-origin smoke'
}
if (Test-Path $workspaceRoot) { Remove-Item -Recurse -Force $workspaceRoot }
New-Item -ItemType Directory -Path $workspaceRoot | Out-Null
if (Test-Path $bundlePath) { Remove-Item -Force $bundlePath }

# Build deterministic local forecast-origin artifacts around the already generated
# governed schema-v2 package fixture. The ledger recorded_at values intentionally
# represent a retrospective September 2026 local run over outcomes revealed in
# April 2026. Canonical origin binding must survive that legitimate product path,
# while physical pre-outcome write/holdout claims remain false without an external
# immutable time anchor.
$originBuilder = @'
import hashlib
import json
import sys
from pathlib import Path

source_bundle_path = Path(sys.argv[1])
bundle_path = Path(sys.argv[2])
workspace_root = Path(sys.argv[3])
dataset_root = Path(sys.argv[4])

raw = json.loads(source_bundle_path.read_text(encoding="utf-8-sig"))
manifest = json.loads((dataset_root / "manifest.json").read_text(encoding="utf-8-sig"))
workspace_root.mkdir(parents=True, exist_ok=True)
ledger_path = workspace_root / "decisions.jsonl"
summary_path = workspace_root / "run-package-origin-smoke.json"

run_id = "package-origin-smoke-run-1"
recorded_at = "2026-09-13T04:58:15+00:00"
records = []
for index, forecast in enumerate(raw["forecasts"], start=1):
    forecast_payload = {
        "forecast_id": forecast["forecast_id"],
        "quote_key": forecast["quote_key"],
        "probability": str(forecast["probability"]),
        "model_id": forecast["model_id"],
        "model_version": forecast["model_version"],
        "strategy_version": forecast["strategy_version"],
        "model_training_cutoff_ts": forecast["model_training_cutoff_ts"],
        "input_cutoff_ts": forecast["input_cutoff_ts"],
        "generated_at": forecast["generated_at"],
        "uncertainty": str(forecast["uncertainty"]),
        "evidence_hashes": list(forecast["evidence_hashes"]),
        "market_snapshot_hash": forecast.get("market_snapshot_hash"),
        "provenance": dict(forecast["provenance"]),
    }
    forecast_canonical = json.dumps(
        forecast_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    forecast_hash = hashlib.sha256(forecast_canonical.encode("utf-8")).hexdigest()
    audit = {
        "quote_key": forecast_payload["quote_key"],
        "forecast_id": forecast_payload["forecast_id"],
        "forecast_hash": forecast_hash,
        "model_id": forecast_payload["model_id"],
        "model_version": forecast_payload["model_version"],
        "strategy_version": forecast_payload["strategy_version"],
        "input_cutoff_ts": forecast_payload["input_cutoff_ts"],
        "generated_at": forecast_payload["generated_at"],
        "uncertainty": forecast_payload["uncertainty"],
    }
    record = {
        "replay_run_id": run_id,
        "agent": "research-decision-pipeline",
        "observed_ts": forecast_payload["generated_at"],
        "action": "REJECT_PAPER_RESEARCH_CANDIDATE",
        "payload": {
            "forecasts": [audit],
            "real_money_execution": False,
        },
        "context_hash": "c" * 64,
        "decision_id": f"package-origin-decision-{index}",
        "recorded_at": recorded_at,
    }
    record_canonical = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    envelope = {
        "sha256": hashlib.sha256(record_canonical.encode("utf-8")).hexdigest(),
        "record": record,
    }
    records.append(json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n")

ledger_bytes = "".join(records).encode("utf-8")
ledger_path.write_bytes(ledger_bytes)
ledger_sha = hashlib.sha256(ledger_bytes).hexdigest()
summary = {
    "schema_version": 2,
    "transaction_schema_version": 1,
    "transaction_run_id": run_id,
    "run_id": run_id,
    "dataset_schema_version": 2,
    "historical_import_identity": manifest["import_identity"],
    "market_sha256": manifest["market_sha256"],
    "sealed_results_sha256": manifest["results_sha256"],
    "decision_ledger_sha256": ledger_sha,
    "strategy_runtime": {"canonical_strategy_id": "research-replay-v1"},
    "real_money_execution": False,
}
summary_path.write_text(
    json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
    newline="\n",
)

raw["forecast_origin"] = {
    "decision_ledger_path": f"{workspace_root.name}/{ledger_path.name}",
    "run_summary_paths": [f"{workspace_root.name}/{summary_path.name}"],
}
bundle_path.write_text(
    json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
    newline="\n",
)
print(json.dumps({
    "decision_ledger_sha256": ledger_sha,
    "historical_import_identity": manifest["import_identity"],
    "market_sha256": manifest["market_sha256"],
    "sealed_results_sha256": manifest["results_sha256"],
    "recorded_at": recorded_at,
}, sort_keys=True, separators=(",", ":")))
'@

$originMetadataJson = $originBuilder | python - $sourceBundlePath $bundlePath $workspaceRoot $datasetRoot
if ($LASTEXITCODE -ne 0) { throw "Forecast-origin fixture generation exited $LASTEXITCODE" }
$originMetadata = $originMetadataJson | ConvertFrom-Json
if ($originMetadata.decision_ledger_sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Forecast-origin fixture ledger SHA is invalid' }
if ($originMetadata.historical_import_identity -notmatch '^[0-9a-f]{64}$') { throw 'Forecast-origin fixture import identity is invalid' }

function Invoke-ForecastOriginPackageSmoke {
  param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$Output,
    [Parameter(Mandatory = $true)][string]$Label
  )

  if (-not (Test-Path $Exe -PathType Leaf)) { throw "$Label Autosport-Data.exe is missing" }
  if (Test-Path $Output) { Remove-Item -Force $Output }
  & $Exe walk-forward-evaluate $bundlePath --output $Output | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "$Label forecast-origin walk-forward evaluation exited $LASTEXITCODE" }
  if (-not (Test-Path $Output -PathType Leaf)) { throw "$Label forecast-origin evaluation did not create a report" }

  $report = Get-Content $Output -Raw | ConvertFrom-Json
  if ($report.kind -ne 'strict_walk_forward_forecast_evaluation') { throw "$Label report kind mismatch" }
  if ($report.input_bundle_schema_version -ne 2) { throw "$Label did not execute schema-v2 governed evaluation" }
  if ($report.evaluation_mode -ne 'governed-historical-canonical-origin-bound-complete-cohort-causal-walk-forward') {
    throw "$Label evaluation_mode is not canonical-origin-bound governed walk-forward"
  }
  if ($report.evaluated_forecast_count -ne 2 -or $report.window_count -ne 2) {
    throw "$Label forecast-origin cohort/window count mismatch"
  }
  if ($report.dataset.historical_import_identity -ne $originMetadata.historical_import_identity) {
    throw "$Label historical_import_identity does not match fixture"
  }
  if ($report.dataset.market_sha256 -ne $originMetadata.market_sha256 -or $report.dataset.sealed_results_sha256 -ne $originMetadata.sealed_results_sha256) {
    throw "$Label governed dataset hashes do not match fixture"
  }

  $truth = $report.truth
  if ($null -eq $truth) { throw "$Label report is missing truth evidence" }
  if ($truth.governed_historical_import -ne $true -or $truth.sealed_dataset_identity_verified -ne $true) {
    throw "$Label did not verify governed sealed dataset identity"
  }
  if ($truth.sealed_outcomes_bound_to_forecasts -ne $true -or $truth.outcome_reveal_boundary_verified -ne $true -or $truth.temporal_timestamp_constraints_verified -ne $true) {
    throw "$Label did not preserve governed causal dataset checks"
  }
  if ($truth.canonical_forecast_origin_verified -ne $true) {
    throw "$Label did not verify canonical forecast origin binding"
  }
  if ($truth.declared_record_time_before_reveal_verified -ne $false) {
    throw "$Label retrospective fixture must report local recorded_at after outcome reveal"
  }
  if ($truth.independent_time_anchor_verified -ne $false -or $truth.pre_outcome_ledger_write_verified -ne $false -or $truth.temporal_holdout_protocol_verified -ne $false) {
    throw "$Label must not promote local retrospective origin artifacts to independent pre-outcome/holdout proof"
  }
  if ($truth.historical_window_market_coverage_verified -ne $false -or $truth.licensing_retention_verified -ne $false) {
    throw "$Label fixture must not claim real historical coverage/licensing proof"
  }
  if ($truth.profitability_claim -ne $false -or $truth.predictive_superiority_claim -ne $false -or $truth.real_money_execution -ne $false) {
    throw "$Label violated nested no-overclaim truth"
  }
  if ($report.profitability_claim -ne $false -or $report.real_money_execution -ne $false) {
    throw "$Label violated top-level no-overclaim truth"
  }

  $origin = $report.forecast_origin
  if ($null -eq $origin -or $origin.status -ne 'CANONICAL_BINDING_VERIFIED') {
    throw "$Label forecast_origin evidence is missing canonical binding status"
  }
  if ($origin.decision_ledger_sha256 -ne $originMetadata.decision_ledger_sha256) {
    throw "$Label forecast-origin ledger identity mismatch"
  }
  if ($origin.evaluated_forecast_count -ne 2 -or $origin.run_summary_count -ne 1) {
    throw "$Label forecast-origin evidence count mismatch"
  }
  if ($origin.declared_record_time_before_reveal_verified -ne $false) {
    throw "$Label origin evidence must preserve retrospective recorded_at truth"
  }
  if ($origin.independent_time_anchor_verified -ne $false -or $origin.pre_outcome_ledger_write_verified -ne $false -or $origin.real_money_execution -ne $false) {
    throw "$Label origin evidence violated physical-time/paper-only truth boundary"
  }
  return $report
}

$preExe = [string]$env:AUTOSPORT_PACKAGED_DATA_EXE
if ([string]::IsNullOrWhiteSpace($preExe)) { throw 'AUTOSPORT_PACKAGED_DATA_EXE verified package extraction anchor is missing' }
$freshExe = Join-Path $PWD '.build-fresh-extraction/Autosport-V1/Autosport-Data.exe'
$preReport = Invoke-ForecastOriginPackageSmoke -Exe $preExe -Output $preReportPath -Label 'Packaged'
$freshReport = Invoke-ForecastOriginPackageSmoke -Exe $freshExe -Output $freshReportPath -Label 'Fresh-extracted'

if ($freshReport.source_sha256 -ne $preReport.source_sha256) {
  throw 'Forecast-origin bundle identity changed between packaged and fresh-extracted execution'
}
if ($freshReport.dataset.historical_import_identity -ne $preReport.dataset.historical_import_identity) {
  throw 'Forecast-origin governed historical identity changed after fresh extraction'
}
if ($freshReport.forecast_origin.decision_ledger_sha256 -ne $preReport.forecast_origin.decision_ledger_sha256) {
  throw 'Forecast-origin decision-ledger identity changed after fresh extraction'
}
if ($freshReport.evaluated_forecast_count -ne $preReport.evaluated_forecast_count -or $freshReport.window_count -ne $preReport.window_count) {
  throw 'Forecast-origin walk-forward evaluation changed after fresh extraction'
}

$verificationPath = Join-Path $PWD 'dist/fresh-extraction-verification.json'
if (-not (Test-Path $verificationPath -PathType Leaf)) { throw 'Fresh-extraction verification evidence is missing' }
$verification = Get-Content $verificationPath -Raw | ConvertFrom-Json
$verification | Add-Member -NotePropertyName extracted_forecast_origin_evaluation_execution_status -NotePropertyValue 'PASS' -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_canonical_binding_verified -NotePropertyValue $true -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_declared_record_time_before_reveal_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_independent_time_anchor_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_pre_outcome_ledger_write_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_temporal_holdout_protocol_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_sample_only -NotePropertyValue $true -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_real_historical_market_proof -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_historical_window_market_coverage_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_licensing_retention_verified -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_profitability_claim -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_predictive_superiority_claim -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_real_money_execution -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_human_tested -NotePropertyValue $false -Force
$verification | Add-Member -NotePropertyName forecast_origin_smoke_nvda_verified -NotePropertyValue $false -Force
$verification | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $verificationPath -Encoding utf8

Write-Host 'Packaged and fresh-extracted canonical forecast-origin walk-forward smoke PASS.'
Write-Host 'canonical_forecast_origin_verified=true'
Write-Host 'declared_record_time_before_reveal_verified=false'
Write-Host 'independent_time_anchor_verified=false'
Write-Host 'pre_outcome_ledger_write_verified=false'
Write-Host 'temporal_holdout_protocol_verified=false'
Write-Host 'profitability_claim=false real_money_execution=false human_tested=false nvda_verified=false'
