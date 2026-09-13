import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.evaluation_bundle import WalkForwardBundle, evaluate_walk_forward_bundle
from autosport.forecasting import ForecastRecord


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_dataset(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    market = root / "market.jsonl"
    results = root / "results.json"
    events = [
        {
            "event_id": "m1",
            "market_id": "winner",
            "selection_id": "a",
            "decimal_odds": "1.80",
            "observed_ts": "2026-02-01T12:00:00+00:00",
            "source_id": "licensed-feed",
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
            "source_id": "licensed-feed",
            "sequence": 2,
            "market_type": "winner",
            "source_ts": "2026-03-01T11:59:59+00:00",
            "ingest_ts": "2026-03-01T12:00:00+00:00",
            "metadata": {},
        },
    ]
    market.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in events),
        encoding="utf-8",
    )
    reveal = "2026-04-01T00:00:00+00:00"
    results.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "outcome_reveal_after": reveal,
                "quote_outcomes": {"m1|winner|a": "win", "m2|winner|b": "loss"},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "dataset_kind": "historical",
        "name": "origin-proof-fixture",
        "sport": "table_tennis",
        "market_file": market.name,
        "results_file": results.name,
        "market_sha256": _sha256(market),
        "results_sha256": _sha256(results),
        "governance": {
            "source_identity": "licensed-feed:origin-fixture",
            "terms_reference": "fixture-contract",
            "retention_basis": "internal test fixture",
            "redistribution_policy": "internal_only",
            "acquired_at": "2026-04-02T00:00:00+00:00",
            "imported_at": "2026-04-02T00:01:00+00:00",
            "coverage": {
                "start_ts": "2026-02-01T00:00:00+00:00",
                "end_ts": "2026-03-31T23:59:59+00:00",
                "source_ids": ["licensed-feed"],
                "market_types": ["winner"],
            },
            "causality": {
                "strategy_time_field": "observed_ts",
                "outcome_reveal_after": reveal,
            },
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return load_dataset(root)


def _forecast(raw: dict) -> ForecastRecord:
    return ForecastRecord(
        quote_key=raw["quote_key"],
        probability=raw["probability"],
        model_id=raw["model_id"],
        model_version=raw["model_version"],
        strategy_version=raw["strategy_version"],
        model_training_cutoff_ts=raw["model_training_cutoff_ts"],
        input_cutoff_ts=raw["input_cutoff_ts"],
        generated_at=raw["generated_at"],
        uncertainty=raw["uncertainty"],
        evidence_hashes=tuple(raw["evidence_hashes"]),
        market_snapshot_hash=raw["market_snapshot_hash"],
        provenance=dict(raw["provenance"]),
        forecast_id=raw["forecast_id"],
    )


def _bundle_raw(dataset) -> dict:
    return {
        "schema_version": 2,
        "dataset": {
            "path": "dataset",
            "historical_import_identity": dataset.import_identity,
            "market_sha256": dataset.market_sha256,
            "sealed_results_sha256": dataset.results_sha256,
        },
        "forecast_origin": {
            "decision_ledger_path": "workspace/decisions.jsonl",
            "run_summary_paths": ["workspace/run-run-1.json"],
        },
        "bins": 5,
        "forecasts": [
            {
                "forecast_id": "f-1",
                "quote_key": "m1|winner|a",
                "probability": "0.70",
                "model_id": "model",
                "model_version": "1",
                "strategy_version": "research-v1",
                "model_training_cutoff_ts": "2026-01-01T00:00:00+00:00",
                "input_cutoff_ts": "2026-02-01T12:00:00+00:00",
                "generated_at": "2026-02-01T12:00:00+00:00",
                "uncertainty": "0.10",
                "evidence_hashes": [],
                "market_snapshot_hash": "a" * 64,
                "provenance": {"source": "fixture"},
            },
            {
                "forecast_id": "f-2",
                "quote_key": "m2|winner|b",
                "probability": "0.30",
                "model_id": "model",
                "model_version": "2",
                "strategy_version": "research-v2",
                "model_training_cutoff_ts": "2026-02-10T00:00:00+00:00",
                "input_cutoff_ts": "2026-03-01T12:00:00+00:00",
                "generated_at": "2026-03-01T12:00:00+00:00",
                "uncertainty": "0.20",
                "evidence_hashes": [],
                "market_snapshot_hash": "b" * 64,
                "provenance": {"source": "fixture"},
            },
        ],
        "outcomes": [
            {"forecast_id": "f-1", "outcome": 1, "revealed_at": "2026-04-01T00:00:00+00:00"},
            {"forecast_id": "f-2", "outcome": 0, "revealed_at": "2026-04-01T00:00:00+00:00"},
        ],
        "windows": [
            {
                "window_id": "holdout-1",
                "training_end_ts": "2026-01-31T23:59:59+00:00",
                "evaluation_start_ts": "2026-02-01T00:00:00+00:00",
                "evaluation_end_ts": "2026-02-28T23:59:59+00:00",
                "split": "holdout",
            },
            {
                "window_id": "holdout-2",
                "training_end_ts": "2026-02-28T23:59:59+00:00",
                "evaluation_start_ts": "2026-03-01T00:00:00+00:00",
                "evaluation_end_ts": "2026-03-31T23:59:59+00:00",
                "split": "holdout",
            },
        ],
    }


def _audit(forecast: ForecastRecord) -> dict:
    return {
        "quote_key": forecast.quote_key,
        "forecast_id": forecast.forecast_id,
        "forecast_hash": forecast.canonical_hash,
        "model_id": forecast.model_id,
        "model_version": forecast.model_version,
        "strategy_version": forecast.strategy_version,
        "input_cutoff_ts": forecast.input_cutoff_ts,
        "generated_at": forecast.generated_at,
        "uncertainty": str(forecast.uncertainty),
    }


def _write_origin(root: Path, dataset, raw: dict, *, second_recorded_at="2026-03-01T12:00:01+00:00", corrupt_hash=False):
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    forecasts = [_forecast(item) for item in raw["forecasts"]]
    records = []
    for index, forecast in enumerate(forecasts):
        audit = _audit(forecast)
        if corrupt_hash and index == 1:
            audit["forecast_hash"] = "0" * 64
        record = {
            "replay_run_id": "run-1",
            "agent": "research-decision-pipeline",
            "observed_ts": forecast.generated_at,
            "action": "REJECT_PAPER_RESEARCH_CANDIDATE",
            "payload": {"forecasts": [audit], "real_money_execution": False},
            "context_hash": "c" * 64,
            "decision_id": f"decision-{index + 1}",
            "recorded_at": "2026-02-01T12:00:01+00:00" if index == 0 else second_recorded_at,
        }
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        records.append(
            json.dumps(
                {"sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "record": record},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    ledger = workspace / "decisions.jsonl"
    ledger.write_text("".join(records), encoding="utf-8", newline="\n")
    summary = {
        "schema_version": 2,
        "transaction_schema_version": 1,
        "transaction_run_id": "run-1",
        "run_id": "run-1",
        "dataset_schema_version": 2,
        "historical_import_identity": dataset.import_identity,
        "market_sha256": dataset.market_sha256,
        "sealed_results_sha256": dataset.results_sha256,
        "decision_ledger_sha256": _sha256(ledger),
        "strategy_runtime": {"canonical_strategy_id": "research-replay-v1"},
        "real_money_execution": False,
    }
    (workspace / "run-run-1.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _write_bundle(root: Path, raw: dict) -> Path:
    path = root / "walk-forward.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class ForecastOriginBindingTests(unittest.TestCase):
    def test_canonical_local_origin_binds_without_overclaiming_physical_write_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_dataset(root / "dataset")
            raw = _bundle_raw(dataset)
            _write_origin(root, dataset, raw)

            report = evaluate_walk_forward_bundle(WalkForwardBundle.from_path(_write_bundle(root, raw)))

            self.assertTrue(report["truth"]["canonical_forecast_origin_verified"])
            self.assertTrue(report["truth"]["declared_record_time_before_reveal_verified"])
            self.assertFalse(report["truth"]["independent_time_anchor_verified"])
            self.assertFalse(report["truth"]["pre_outcome_ledger_write_verified"])
            self.assertFalse(report["truth"]["temporal_holdout_protocol_verified"])
            self.assertEqual(report["forecast_origin"]["status"], "CANONICAL_BINDING_VERIFIED")
            self.assertEqual(report["forecast_origin"]["evaluated_forecast_count"], 2)
            self.assertFalse(report["forecast_origin"]["pre_outcome_ledger_write_verified"])
            self.assertFalse(report["forecast_origin"]["independent_time_anchor_verified"])
            self.assertFalse(report["truth"]["historical_window_market_coverage_verified"])
            self.assertFalse(report["truth"]["licensing_retention_verified"])
            self.assertFalse(report["truth"]["profitability_claim"])

    def test_declared_post_reveal_record_time_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_dataset(root / "dataset")
            raw = _bundle_raw(dataset)
            _write_origin(root, dataset, raw, second_recorded_at="2026-04-01T00:00:00+00:00")

            with self.assertRaisesRegex(ValueError, "at or after outcome reveal"):
                evaluate_walk_forward_bundle(WalkForwardBundle.from_path(_write_bundle(root, raw)))

    def test_forged_forecast_hash_in_valid_ledger_envelope_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_dataset(root / "dataset")
            raw = _bundle_raw(dataset)
            _write_origin(root, dataset, raw, corrupt_hash=True)

            with self.assertRaisesRegex(ValueError, "forecast audit mismatch"):
                evaluate_walk_forward_bundle(WalkForwardBundle.from_path(_write_bundle(root, raw)))

    def test_missing_origin_declaration_preserves_all_origin_truth_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_dataset(root / "dataset")
            raw = _bundle_raw(dataset)
            raw.pop("forecast_origin")

            report = evaluate_walk_forward_bundle(WalkForwardBundle.from_path(_write_bundle(root, raw)))

            self.assertFalse(report["truth"]["canonical_forecast_origin_verified"])
            self.assertFalse(report["truth"]["declared_record_time_before_reveal_verified"])
            self.assertFalse(report["truth"]["independent_time_anchor_verified"])
            self.assertFalse(report["truth"]["pre_outcome_ledger_write_verified"])
            self.assertFalse(report["truth"]["temporal_holdout_protocol_verified"])


if __name__ == "__main__":
    unittest.main()
