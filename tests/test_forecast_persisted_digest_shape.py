import unittest

from autosport.evaluation_bundle import _forecast_from_dict as evaluation_forecast_from_dict
from autosport.research_strategy import _forecast_from_dict as research_forecast_from_dict


class PersistedForecastDigestShapeTests(unittest.TestCase):
    def _raw(self, **overrides):
        values = {
            "quote_key": "event|winner|selection",
            "probability": "0.55",
            "model_id": "model",
            "model_version": "1",
            "strategy_version": "research-v1",
            "model_training_cutoff_ts": "2026-01-01T00:00:00+00:00",
            "input_cutoff_ts": "2026-02-01T11:59:00+00:00",
            "generated_at": "2026-02-01T12:00:00+00:00",
            "uncertainty": "0.10",
            "evidence_hashes": ["a" * 64],
            "market_snapshot_hash": "b" * 64,
            "provenance": {"source": "fixture"},
            "forecast_id": "forecast-1",
        }
        values.update(overrides)
        return values

    def _loaders(self):
        return (
            ("research_strategy", research_forecast_from_dict),
            ("evaluation_bundle", evaluation_forecast_from_dict),
        )

    def test_canonical_json_array_is_preserved_as_immutable_tuple(self):
        for name, loader in self._loaders():
            with self.subTest(loader=name):
                record = loader(self._raw())
                self.assertEqual(record.evidence_hashes, ("a" * 64,))
                self.assertIsInstance(record.evidence_hashes, tuple)

    def test_scalar_string_cannot_be_iterated_into_persisted_evidence_hashes(self):
        for name, loader in self._loaders():
            with self.subTest(loader=name):
                with self.assertRaisesRegex(ValueError, "evidence_hashes must be a JSON array"):
                    loader(self._raw(evidence_hashes="a" * 64))

    def test_mapping_keys_cannot_bypass_json_array_shape_validation(self):
        digest = "a" * 64
        for name, loader in self._loaders():
            with self.subTest(loader=name):
                with self.assertRaisesRegex(ValueError, "evidence_hashes must be a JSON array"):
                    loader(self._raw(evidence_hashes={digest: "payload"}))

    def test_non_string_json_array_members_fail_before_digest_coercion(self):
        for name, loader in self._loaders():
            with self.subTest(loader=name):
                with self.assertRaisesRegex(ValueError, "evidence_hashes must contain strings"):
                    loader(self._raw(evidence_hashes=[123]))

    def test_non_string_snapshot_hash_is_not_stringified_by_persisted_loader(self):
        for name, loader in self._loaders():
            with self.subTest(loader=name):
                with self.assertRaisesRegex(ValueError, "market_snapshot_hash.*SHA-256"):
                    loader(self._raw(market_snapshot_hash=7))


if __name__ == "__main__":
    unittest.main()
