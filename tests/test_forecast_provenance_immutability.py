import json
import tempfile
import unittest
from pathlib import Path

from autosport.forecasting import ForecastRecord, JsonlForecastLedger


class ForecastProvenanceImmutabilityTests(unittest.TestCase):
    def _record(self, provenance: dict[str, object]) -> ForecastRecord:
        return ForecastRecord(
            quote_key="event|winner|selection-a",
            probability="0.6",
            model_id="model-a",
            model_version="1",
            strategy_version="strategy-a",
            model_training_cutoff_ts="2026-01-01T00:00:00+00:00",
            input_cutoff_ts="2026-01-02T00:00:00+00:00",
            generated_at="2026-01-02T00:01:00+00:00",
            provenance=provenance,
            forecast_id="forecast-immutability",
        )

    def test_constructor_rejects_nested_future_result_fields(self):
        for key in ("result", "winner", "outcome"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "future-result fields"):
                    self._record({"nested": [{key: "selection-b"}]})

    def test_input_alias_cannot_inject_future_result_or_change_hash(self):
        original = {"nested": [{"feature": "serve-form"}]}
        record = self._record(original)
        canonical_hash = record.canonical_hash

        original["winner"] = "selection-b"
        original["nested"][0]["result"] = "selection-b"

        self.assertNotIn("winner", record.provenance)
        self.assertNotIn("result", record.provenance["nested"][0])
        self.assertEqual(record.canonical_hash, canonical_hash)

    def test_validated_provenance_is_structurally_immutable_even_via_builtin_base_methods(self):
        record = self._record({"nested": [{"feature": "serve-form"}]})
        canonical_hash = record.canonical_hash

        self.assertNotIsInstance(record.provenance, dict)
        self.assertNotIsInstance(record.provenance["nested"][0], dict)
        self.assertIsInstance(record.provenance["nested"], tuple)
        with self.assertRaises(TypeError):
            dict.__setitem__(record.provenance, "winner", "selection-b")
        with self.assertRaises(TypeError):
            dict.__setitem__(record.provenance["nested"][0], "result", "selection-b")
        with self.assertRaises(TypeError):
            list.append(record.provenance["nested"], {"result": "selection-b"})

        self.assertNotIn("winner", record.provenance)
        self.assertNotIn("result", record.provenance["nested"][0])
        self.assertEqual(record.canonical_hash, canonical_hash)

    def test_to_dict_returns_detached_json_container_and_ledger_stays_causal(self):
        record = self._record({"nested": [{"feature": "serve-form"}]})
        canonical_hash = record.canonical_hash
        exported = record.to_dict()

        self.assertIsInstance(exported["provenance"]["nested"], list)
        exported["provenance"]["winner"] = "selection-b"
        exported["provenance"]["nested"][0]["result"] = "selection-b"

        self.assertNotIn("winner", record.provenance)
        self.assertNotIn("result", record.provenance["nested"][0])
        self.assertEqual(record.canonical_hash, canonical_hash)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forecast.jsonl"
            digest = JsonlForecastLedger(path).append(record)
            envelope = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(digest, canonical_hash)
        self.assertNotIn("winner", envelope["record"]["provenance"])
        self.assertNotIn("result", envelope["record"]["provenance"]["nested"][0])


if __name__ == "__main__":
    unittest.main()
