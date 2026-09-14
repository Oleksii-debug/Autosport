import json
import tempfile
import unittest
from pathlib import Path

from autosport.evaluation_bundle import WalkForwardBundle


class WalkForwardBundleInputIntegrityTests(unittest.TestCase):
    def _valid_raw(self):
        return {
            "schema_version": 1,
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
                }
            ],
            "outcomes": [
                {
                    "forecast_id": "f-1",
                    "outcome": 1,
                    "revealed_at": "2026-02-02T12:00:00+00:00",
                }
            ],
            "windows": [
                {
                    "window_id": "holdout-1",
                    "training_end_ts": "2026-01-31T23:59:59+00:00",
                    "evaluation_start_ts": "2026-02-01T00:00:00+00:00",
                    "evaluation_end_ts": "2026-02-28T23:59:59+00:00",
                    "split": "holdout",
                }
            ],
        }

    def test_from_path_rejects_duplicate_json_object_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bundle.json"
            source.write_text(
                '{"schema_version":1,"schema_version":1}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: schema_version"):
                WalkForwardBundle.from_path(source)

    def test_from_path_rejects_nonstandard_json_constant(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bundle.json"
            source.write_text('{"schema_version":1,"bins":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-standard JSON constant: NaN"):
                WalkForwardBundle.from_path(source)

    def test_schema_version_requires_exact_integer_type(self):
        for value in (True, 1.0, "1", 2.0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "schema_version must be 1 or 2"):
                    WalkForwardBundle.from_dict({"schema_version": value})

    def test_non_integer_schema_two_does_not_attempt_dataset_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bundle.json"
            source.write_text(
                json.dumps(
                    {
                        "schema_version": 2.0,
                        "dataset": {"path": "missing-dataset.json"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "schema_version must be 1 or 2"):
                WalkForwardBundle.from_path(source)

    def test_bins_requires_exact_positive_integer_type(self):
        for value in (True, 5.0, "5", 1.9):
            with self.subTest(value=value):
                raw = self._valid_raw()
                raw["bins"] = value
                with self.assertRaisesRegex(ValueError, "bins must be a positive integer"):
                    WalkForwardBundle.from_dict(raw)

    def test_missing_bins_retains_default(self):
        bundle = WalkForwardBundle.from_dict(self._valid_raw())
        self.assertEqual(bundle.bins, 10)


if __name__ == "__main__":
    unittest.main()
