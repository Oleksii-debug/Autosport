import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.evidence import EvidenceItem
from autosport.forecasting import ForecastRecord, JsonlForecastLedger


class CausalJsonDomainIntegrityTests(unittest.TestCase):
    @staticmethod
    def _evidence(payload):
        return EvidenceItem(
            evidence_id="evidence-json-domain",
            as_of_ts="2026-01-01T00:00:00+00:00",
            source="test-source",
            kind="research",
            payload=payload,
        )

    @staticmethod
    def _forecast(provenance):
        return ForecastRecord(
            quote_key="event|winner|selection-a",
            probability=Decimal("0.6"),
            model_id="model-a",
            model_version="1",
            strategy_version="strategy-a",
            model_training_cutoff_ts="2026-01-01T00:00:00+00:00",
            input_cutoff_ts="2026-01-02T00:00:00+00:00",
            generated_at="2026-01-02T00:01:00+00:00",
            provenance=provenance,
            forecast_id="forecast-json-domain",
        )

    def test_top_level_causal_provenance_must_be_json_object(self):
        for value in ([], (), ["feature"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                    self._evidence(value)
                with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                    self._forecast(value)

    def test_non_string_object_keys_are_rejected_before_hash_binding(self):
        for value in ({1: "x"}, {"nested": {1: "x"}}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "keys must be strings"):
                    self._evidence(value)
                with self.assertRaisesRegex(ValueError, "keys must be strings"):
                    self._forecast(value)

    def test_non_finite_numbers_are_rejected_before_hash_binding(self):
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                payload = {"nested": [number]}
                with self.assertRaisesRegex(ValueError, "finite JSON numbers"):
                    self._evidence(payload)
                with self.assertRaisesRegex(ValueError, "finite JSON numbers"):
                    self._forecast(payload)

    def test_non_json_values_are_rejected_before_hash_binding(self):
        payload = {"nested": Decimal("1.25")}
        with self.assertRaisesRegex(ValueError, "unsupported JSON value type Decimal"):
            self._evidence(payload)
        with self.assertRaisesRegex(ValueError, "unsupported JSON value type Decimal"):
            self._forecast(payload)

    def test_valid_finite_json_remains_hashable_and_persistable(self):
        payload = {
            "feature": "serve-form",
            "nested": [{"score": 1.25, "active": True, "missing": None}],
        }
        evidence = self._evidence(payload)
        forecast = self._forecast(payload)

        self.assertEqual(len(evidence.canonical_hash), 64)
        self.assertEqual(len(forecast.canonical_hash), 64)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forecasts.jsonl"
            digest = JsonlForecastLedger(path).append(forecast)
            envelope = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(envelope["sha256"], digest)
            self.assertEqual(envelope["record"]["provenance"], payload)


if __name__ == "__main__":
    unittest.main()
