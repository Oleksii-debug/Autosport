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

    def test_from_path_rejects_standard_numeric_overflow_before_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bundle.json"
            payload = json.dumps(
                self._valid_raw(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).replace(
                '"provenance":{"source":"fixture"}',
                '"provenance":{"source":"fixture","weight":1e400}',
            )
            self.assertIn("1e400", payload)

            source.write_text(payload, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
                WalkForwardBundle.from_path(source)

    def test_from_path_rejects_escaped_lone_surrogate_in_value_or_key(self):
        replacements = (
            '"provenance":{"source":"\\ud800"}',
            '"provenance":{"\\ud800":"fixture"}',
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / "bundle.json"
                    payload = json.dumps(
                        self._valid_raw(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).replace(
                        '"provenance":{"source":"fixture"}',
                        replacement,
                    )

                    source.write_text(payload, encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, "invalid UTF-8 text"):
                        WalkForwardBundle.from_path(source)

    def test_from_path_normalizes_excessive_json_nesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bundle.json"
            payload = (
                '{"schema_version":1,"nested":'
                + "[" * 10000
                + "0"
                + "]" * 10000
                + "}"
            )
            source.write_text(payload, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSON nesting is too deep"):
                WalkForwardBundle.from_path(source)

    def test_from_path_rejects_deep_parsable_provenance_before_forecast_construction(self):
        raw = self._valid_raw()
        nested = "leaf"
        for _ in range(160):
            nested = [nested]
        raw["forecasts"][0]["provenance"] = {"nested": nested}

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bundle.json"
            source.write_text(
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "JSON nesting is too deep"):
                WalkForwardBundle.from_path(source)

    def test_from_path_preserves_finite_nested_numbers_and_valid_unicode(self):
        raw = self._valid_raw()
        raw["forecasts"][0]["provenance"] = {
            "source": "фікстура",
            "weight": 1e300,
            "nested": {"label": "✅"},
        }

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bundle.json"
            source.write_text(
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

            bundle = WalkForwardBundle.from_path(source)

        self.assertEqual(bundle.forecasts[0].provenance["source"], "фікстура")
        self.assertEqual(bundle.forecasts[0].provenance["weight"], 1e300)
        self.assertEqual(bundle.forecasts[0].provenance["nested"]["label"], "✅")

    def test_from_dict_rejects_nonfinite_json_domain_even_with_supplied_hash(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                raw = self._valid_raw()
                raw["forecasts"][0]["provenance"] = {"weight": value}
                with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
                    WalkForwardBundle.from_dict(raw, source_sha256="0" * 64)

    def test_from_dict_rejects_lone_surrogate_value_or_key(self):
        provenances = (
            {"source": "\ud800"},
            {"\ud800": "fixture"},
        )
        for provenance in provenances:
            with self.subTest(provenance=repr(provenance)):
                raw = self._valid_raw()
                raw["forecasts"][0]["provenance"] = provenance
                with self.assertRaisesRegex(ValueError, "invalid UTF-8 text"):
                    WalkForwardBundle.from_dict(raw, source_sha256="0" * 64)

    def test_from_dict_rejects_non_json_value_or_mapping_key(self):
        cases = (
            ({"source": ("fixture",)}, "non-JSON value"),
            ({1: "fixture"}, "non-string JSON object key"),
        )
        for provenance, message in cases:
            with self.subTest(message=message):
                raw = self._valid_raw()
                raw["forecasts"][0]["provenance"] = provenance
                with self.assertRaisesRegex(ValueError, message):
                    WalkForwardBundle.from_dict(raw, source_sha256="0" * 64)

    def test_from_dict_rejects_excessive_nesting_before_forecast_construction(self):
        raw = self._valid_raw()
        nested = "leaf"
        for _ in range(160):
            nested = [nested]
        raw["forecasts"][0]["provenance"] = {"nested": nested}

        with self.assertRaisesRegex(ValueError, "JSON nesting is too deep"):
            WalkForwardBundle.from_dict(raw, source_sha256="0" * 64)

    def test_from_dict_preserves_finite_json_scalars_and_valid_unicode(self):
        raw = self._valid_raw()
        raw["forecasts"][0]["provenance"] = {
            "source": "фікстура",
            "weight": 1e300,
            "nested": {"label": "✅", "flag": True, "missing": None, "count": 2},
        }

        bundle = WalkForwardBundle.from_dict(raw)

        self.assertEqual(bundle.forecasts[0].provenance["source"], "фікстура")
        self.assertEqual(bundle.forecasts[0].provenance["weight"], 1e300)
        self.assertEqual(bundle.forecasts[0].provenance["nested"]["label"], "✅")
        self.assertIs(bundle.forecasts[0].provenance["nested"]["flag"], True)
        self.assertIsNone(bundle.forecasts[0].provenance["nested"]["missing"])
        self.assertEqual(bundle.forecasts[0].provenance["nested"]["count"], 2)

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

    def test_forecast_causal_text_fields_reject_type_coercion(self):
        fields = (
            "forecast_id",
            "quote_key",
            "model_id",
            "model_version",
            "strategy_version",
            "model_training_cutoff_ts",
            "input_cutoff_ts",
            "generated_at",
        )
        for field in fields:
            for value in (True, 7, 1.5):
                with self.subTest(field=field, value=value):
                    raw = self._valid_raw()
                    raw["forecasts"][0][field] = value
                    with self.assertRaisesRegex(ValueError, field):
                        WalkForwardBundle.from_dict(raw)

    def test_forecast_causal_text_fields_reject_noncanonical_whitespace(self):
        for field in ("forecast_id", "quote_key", "model_id", "strategy_version"):
            with self.subTest(field=field):
                raw = self._valid_raw()
                raw["forecasts"][0][field] = f" {raw['forecasts'][0][field]} "
                with self.assertRaisesRegex(ValueError, field):
                    WalkForwardBundle.from_dict(raw)

    def test_forecast_decimal_fields_reject_type_coercion(self):
        for field in ("probability", "uncertainty"):
            for value in (True, False, 0, 1, 0.5, [], {}):
                with self.subTest(field=field, value=value):
                    raw = self._valid_raw()
                    raw["forecasts"][0][field] = value
                    with self.assertRaisesRegex(ValueError, field):
                        WalkForwardBundle.from_dict(raw)

    def test_forecast_decimal_fields_normalize_malformed_text_to_value_error(self):
        for field in ("probability", "uncertainty"):
            for value in ("not-a-decimal", "", " 0.5 "):
                with self.subTest(field=field, value=value):
                    raw = self._valid_raw()
                    raw["forecasts"][0][field] = value
                    with self.assertRaisesRegex(ValueError, field):
                        WalkForwardBundle.from_dict(raw)

    def test_missing_uncertainty_retains_canonical_default(self):
        raw = self._valid_raw()
        del raw["forecasts"][0]["uncertainty"]

        bundle = WalkForwardBundle.from_dict(raw)

        self.assertEqual(str(bundle.forecasts[0].uncertainty), "0")

    def test_forecast_provenance_requires_json_object(self):
        raw = self._valid_raw()
        raw["forecasts"][0]["provenance"] = [["source", "fixture"]]

        with self.assertRaisesRegex(ValueError, "provenance must be a JSON object"):
            WalkForwardBundle.from_dict(raw)

    def test_outcome_requires_exact_non_boolean_integer(self):
        for value in (True, False, "1", "0", 1.0, 0.0, -1, 2):
            with self.subTest(value=value):
                raw = self._valid_raw()
                raw["outcomes"][0]["outcome"] = value
                with self.assertRaisesRegex(ValueError, "integer 0 or 1"):
                    WalkForwardBundle.from_dict(raw)

    def test_outcome_zero_and_one_remain_valid(self):
        for value in (0, 1):
            with self.subTest(value=value):
                raw = self._valid_raw()
                raw["outcomes"][0]["outcome"] = value
                bundle = WalkForwardBundle.from_dict(raw)
                self.assertEqual(bundle.outcomes[0].outcome, value)
                self.assertIs(type(bundle.outcomes[0].outcome), int)

    def test_outcome_identity_and_timestamp_reject_type_coercion(self):
        for field in ("forecast_id", "revealed_at"):
            for value in (True, 7, 1.5):
                with self.subTest(field=field, value=value):
                    raw = self._valid_raw()
                    raw["outcomes"][0][field] = value
                    with self.assertRaisesRegex(ValueError, field):
                        WalkForwardBundle.from_dict(raw)

    def test_window_text_fields_reject_type_coercion(self):
        fields = (
            "window_id",
            "training_end_ts",
            "evaluation_start_ts",
            "evaluation_end_ts",
            "split",
        )
        for field in fields:
            for value in (True, 7, 1.5):
                with self.subTest(field=field, value=value):
                    raw = self._valid_raw()
                    raw["windows"][0][field] = value
                    with self.assertRaisesRegex(ValueError, field):
                        WalkForwardBundle.from_dict(raw)

    def test_valid_bundle_preserves_exact_causal_text(self):
        raw = self._valid_raw()
        bundle = WalkForwardBundle.from_dict(raw)

        self.assertEqual(bundle.forecasts[0].forecast_id, "f-1")
        self.assertEqual(bundle.forecasts[0].quote_key, "m1|winner|a")
        self.assertEqual(str(bundle.forecasts[0].probability), "0.70")
        self.assertEqual(str(bundle.forecasts[0].uncertainty), "0.10")
        self.assertEqual(bundle.outcomes[0].forecast_id, "f-1")
        self.assertEqual(bundle.windows[0].window_id, "holdout-1")
        self.assertEqual(bundle.windows[0].split, "holdout")


if __name__ == "__main__":
    unittest.main()
