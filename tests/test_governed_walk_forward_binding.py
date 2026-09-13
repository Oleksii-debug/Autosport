import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.evaluation_bundle import WalkForwardBundle, evaluate_walk_forward_bundle


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_governed_dataset(root: Path, *, second_outcome: str = "loss"):
    root.mkdir(parents=True, exist_ok=True)
    market_path = root / "market.jsonl"
    results_path = root / "results.json"
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
    market_path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in events),
        encoding="utf-8",
    )
    reveal_after = "2026-04-01T00:00:00+00:00"
    results_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "outcome_reveal_after": reveal_after,
                "quote_outcomes": {
                    "m1|winner|a": "win",
                    "m2|winner|b": second_outcome,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "dataset_kind": "historical",
        "name": "governed walk-forward sample",
        "sport": "table_tennis",
        "market_file": market_path.name,
        "results_file": results_path.name,
        "market_sha256": _sha256(market_path),
        "results_sha256": _sha256(results_path),
        "governance": {
            "source_identity": "licensed-feed:walk-forward-fixture",
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
                "outcome_reveal_after": reveal_after,
            },
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return load_dataset(root)


def _raw_bundle(dataset) -> dict:
    return {
        "schema_version": 2,
        "dataset": {
            "path": "dataset",
            "historical_import_identity": dataset.import_identity,
            "market_sha256": dataset.market_sha256,
            "sealed_results_sha256": dataset.results_sha256,
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
            {
                "forecast_id": "f-1",
                "outcome": 1,
                "revealed_at": "2026-04-01T00:00:00+00:00",
            },
            {
                "forecast_id": "f-2",
                "outcome": 0,
                "revealed_at": "2026-04-01T00:00:00+00:00",
            },
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


def _write_bundle(root: Path, raw: dict) -> Path:
    path = root / "walk-forward.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class GovernedWalkForwardBindingTests(unittest.TestCase):
    def test_schema_v2_binds_metrics_to_exact_governed_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset")
            bundle = WalkForwardBundle.from_path(_write_bundle(root, _raw_bundle(dataset)))

            report = evaluate_walk_forward_bundle(bundle)

            self.assertEqual(
                report["evaluation_mode"],
                "governed-historical-complete-cohort-causal-walk-forward",
            )
            self.assertEqual(report["dataset"]["historical_import_identity"], dataset.import_identity)
            self.assertEqual(report["dataset"]["market_sha256"], dataset.market_sha256)
            self.assertEqual(report["dataset"]["sealed_results_sha256"], dataset.results_sha256)
            self.assertTrue(report["truth"]["governed_historical_import"])
            self.assertTrue(report["truth"]["sealed_dataset_identity_verified"])
            self.assertTrue(report["truth"]["sealed_outcomes_bound_to_forecasts"])
            self.assertTrue(report["truth"]["outcome_reveal_boundary_verified"])
            self.assertTrue(report["truth"]["temporal_timestamp_constraints_verified"])
            self.assertFalse(report["truth"]["temporal_holdout_protocol_verified"])
            self.assertFalse(report["truth"]["historical_window_market_coverage_verified"])
            self.assertFalse(report["truth"]["licensing_retention_verified"])
            self.assertFalse(report["truth"]["profitability_claim"])

    def test_outcome_perfect_backdated_json_does_not_claim_verified_holdout_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset")
            raw = _raw_bundle(dataset)
            # Simulate a post-outcome author writing outcome-perfect probabilities
            # while backdating otherwise valid forecast timestamps. Without a
            # durable pre-outcome forecast-origin proof, this must never promote
            # temporal_holdout_protocol_verified.
            raw["forecasts"][0]["probability"] = "0.999"
            raw["forecasts"][1]["probability"] = "0.001"
            raw["forecasts"][0]["provenance"] = {"fixture_attack": "authored_after_reveal"}
            raw["forecasts"][1]["provenance"] = {"fixture_attack": "authored_after_reveal"}
            bundle = WalkForwardBundle.from_path(_write_bundle(root, raw))

            report = evaluate_walk_forward_bundle(bundle)

            self.assertTrue(report["truth"]["temporal_timestamp_constraints_verified"])
            self.assertFalse(report["truth"]["temporal_holdout_protocol_verified"])
            self.assertFalse(report["truth"]["predictive_superiority_claim"])
            self.assertFalse(report["truth"]["profitability_claim"])

    def test_declared_historical_import_identity_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset")
            raw = _raw_bundle(dataset)
            raw["dataset"]["historical_import_identity"] = "0" * 64

            with self.assertRaisesRegex(ValueError, "historical_import_identity"):
                WalkForwardBundle.from_path(_write_bundle(root, raw))

    def test_forecast_quote_absent_from_governed_corpus_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset")
            raw = _raw_bundle(dataset)
            raw["forecasts"][1]["quote_key"] = "foreign|winner|x"
            bundle = WalkForwardBundle.from_path(_write_bundle(root, raw))

            with self.assertRaisesRegex(ValueError, "absent from governed historical corpus"):
                evaluate_walk_forward_bundle(bundle)

    def test_forecast_input_cutoff_before_first_quote_observation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset")
            raw = _raw_bundle(dataset)
            raw["forecasts"][1]["input_cutoff_ts"] = "2026-02-20T12:00:00+00:00"
            bundle = WalkForwardBundle.from_path(_write_bundle(root, raw))

            with self.assertRaisesRegex(ValueError, "input cutoff predates"):
                evaluate_walk_forward_bundle(bundle)

    def test_outcome_fact_must_match_governed_sealed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset")
            raw = _raw_bundle(dataset)
            raw["outcomes"][1]["outcome"] = 1
            bundle = WalkForwardBundle.from_path(_write_bundle(root, raw))

            with self.assertRaisesRegex(ValueError, "disagrees with governed sealed outcome"):
                evaluate_walk_forward_bundle(bundle)

    def test_outcome_fact_must_use_governed_reveal_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset")
            raw = _raw_bundle(dataset)
            raw["outcomes"][0]["revealed_at"] = "2026-04-02T00:00:00+00:00"
            bundle = WalkForwardBundle.from_path(_write_bundle(root, raw))

            with self.assertRaisesRegex(ValueError, "reveal timestamp"):
                evaluate_walk_forward_bundle(bundle)

    def test_void_sealed_outcome_is_not_silently_coerced_into_binary_metric(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset", second_outcome="void")
            bundle = WalkForwardBundle.from_path(_write_bundle(root, _raw_bundle(dataset)))

            with self.assertRaisesRegex(ValueError, "cannot score a void"):
                evaluate_walk_forward_bundle(bundle)

    def test_governed_dataset_path_escape_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_governed_dataset(root / "dataset")
            raw = _raw_bundle(dataset)
            raw["dataset"]["path"] = "../outside"

            with self.assertRaisesRegex(ValueError, "escapes the bundle directory"):
                WalkForwardBundle.from_path(_write_bundle(root, raw))


if __name__ == "__main__":
    unittest.main()
