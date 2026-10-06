import unittest
from decimal import Decimal

from autosport.forecasting import ForecastRecord


class ForecastDigestCanonicalTests(unittest.TestCase):
    def _record(self, **overrides) -> ForecastRecord:
        values = {
            "quote_key": "event|winner|selection",
            "probability": Decimal("0.55"),
            "model_id": "model",
            "model_version": "1",
            "strategy_version": "research-v1",
            "model_training_cutoff_ts": "2026-01-01T00:00:00+00:00",
            "input_cutoff_ts": "2026-02-01T11:59:00+00:00",
            "generated_at": "2026-02-01T12:00:00+00:00",
            "evidence_hashes": ("a" * 64,),
            "market_snapshot_hash": "b" * 64,
            "provenance": {"source": "fixture"},
            "forecast_id": "forecast-1",
        }
        values.update(overrides)
        return ForecastRecord(**values)

    def test_scalar_evidence_hash_string_fails_closed_before_character_iteration(self) -> None:
        with self.assertRaisesRegex(ValueError, "ordered collection"):
            self._record(evidence_hashes="a" * 64)

    def test_only_ordered_tuple_or_list_evidence_hash_containers_are_accepted(self) -> None:
        unsupported = [
            {"a" * 64},
            frozenset({"a" * 64}),
            {"digest": "a" * 64},
            (value for value in ("a" * 64,)),
            b"a" * 64,
        ]
        for value in unsupported:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "ordered collection"):
                    self._record(evidence_hashes=value)

    def test_canonical_list_normalizes_to_immutable_tuple(self) -> None:
        record = self._record(evidence_hashes=["a" * 64, "c" * 64])
        self.assertEqual(record.evidence_hashes, ("a" * 64, "c" * 64))
        self.assertIsInstance(record.evidence_hashes, tuple)

    def test_evidence_members_must_be_canonical_lowercase_sha256(self) -> None:
        invalid = [
            "A" * 64,
            "a" * 63,
            "a" * 65,
            "g" * 64,
            " " + ("a" * 63),
            7,
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "canonical.*SHA-256"):
                    self._record(evidence_hashes=(value,))

    def test_duplicate_evidence_hashes_still_fail_closed(self) -> None:
        digest = "a" * 64
        with self.assertRaisesRegex(ValueError, "duplicate evidence hashes"):
            self._record(evidence_hashes=(digest, digest))

    def test_market_snapshot_hash_is_nullable_but_must_be_canonical_when_present(self) -> None:
        self.assertIsNone(self._record(market_snapshot_hash=None).market_snapshot_hash)
        for value in ("B" * 64, "b" * 63, "z" * 64, 7):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "market_snapshot_hash.*SHA-256"):
                    self._record(market_snapshot_hash=value)


if __name__ == "__main__":
    unittest.main()
