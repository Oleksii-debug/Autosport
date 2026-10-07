import json
import tempfile
import unittest
from pathlib import Path

from autosport.cli import run_walk_forward_evaluate
from autosport.evaluation_bundle import WalkForwardBundle, evaluate_walk_forward_bundle


class WalkForwardProductTests(unittest.TestCase):
    def _raw(self):
        return {
            "schema_version": 1,
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
                    "revealed_at": "2026-02-02T12:00:00+00:00",
                },
                {
                    "forecast_id": "f-2",
                    "outcome": 0,
                    "revealed_at": "2026-03-02T12:00:00+00:00",
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

    def test_complete_cohort_walk_forward_report(self):
        bundle = WalkForwardBundle.from_dict(self._raw())
        report = evaluate_walk_forward_bundle(bundle)
        self.assertEqual(report["evaluation_mode"], "complete-cohort-causal-walk-forward")
        self.assertEqual(report["evaluated_forecast_count"], 2)
        self.assertEqual(report["window_count"], 2)
        self.assertEqual([item["count"] for item in report["windows"]], [1, 1])
        self.assertFalse(report["profitability_claim"])
        self.assertFalse(report["real_money_execution"])

    def test_missing_outcome_inside_holdout_fails_closed(self):
        raw = self._raw()
        raw["outcomes"] = raw["outcomes"][:1]
        bundle = WalkForwardBundle.from_dict(raw)
        with self.assertRaisesRegex(ValueError, "cohort is incomplete"):
            evaluate_walk_forward_bundle(bundle)

    def test_training_cutoff_crossing_fold_boundary_fails_closed(self):
        raw = self._raw()
        raw["forecasts"][0]["model_training_cutoff_ts"] = "2026-02-01T01:00:00+00:00"
        bundle = WalkForwardBundle.from_dict(raw)
        with self.assertRaisesRegex(ValueError, "training cutoff crosses evaluation boundary"):
            evaluate_walk_forward_bundle(bundle)

    def test_unknown_outcome_identity_is_rejected(self):
        raw = self._raw()
        raw["outcomes"].append(
            {
                "forecast_id": "not-a-forecast",
                "outcome": 1,
                "revealed_at": "2026-03-03T00:00:00+00:00",
            }
        )
        with self.assertRaisesRegex(ValueError, "unknown forecasts"):
            WalkForwardBundle.from_dict(raw)

    def test_cli_writes_machine_verifiable_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "walk-forward.json"
            output = root / "report.json"
            source.write_text(json.dumps(self._raw()), encoding="utf-8")
            self.assertEqual(run_walk_forward_evaluate(source, output), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["kind"], "strict_walk_forward_forecast_evaluation")
            self.assertEqual(payload["evaluated_forecast_count"], 2)
            self.assertEqual(len(payload["source_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
