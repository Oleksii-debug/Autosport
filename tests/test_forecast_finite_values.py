import unittest
from decimal import Decimal

from autosport.forecasting import ForecastRecord


class ForecastFiniteValueTests(unittest.TestCase):
    @staticmethod
    def _record(**overrides) -> ForecastRecord:
        values = {
            "quote_key": "event|winner|selection",
            "probability": Decimal("0.55"),
            "model_id": "model",
            "model_version": "1",
            "strategy_version": "research-v1",
            "model_training_cutoff_ts": "2026-01-01T00:00:00+00:00",
            "input_cutoff_ts": "2026-02-01T11:59:00+00:00",
            "generated_at": "2026-02-01T12:00:00+00:00",
            "uncertainty": Decimal("0.10"),
            "evidence_hashes": ("a" * 64,),
            "market_snapshot_hash": "b" * 64,
            "provenance": {"source": "fixture"},
            "forecast_id": "forecast-finite-values",
        }
        values.update(overrides)
        return ForecastRecord(**values)

    def test_non_finite_probability_fails_closed_with_value_error(self) -> None:
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                with self.assertRaisesRegex(ValueError, "probability must be finite"):
                    self._record(probability=value)

    def test_non_finite_uncertainty_fails_closed_with_value_error(self) -> None:
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                with self.assertRaisesRegex(ValueError, "uncertainty must be finite"):
                    self._record(uncertainty=value)

    def test_finite_boundaries_remain_valid(self) -> None:
        zero = self._record(probability=Decimal("0"), uncertainty=Decimal("0"))
        one = self._record(probability=Decimal("1"), uncertainty=Decimal("1"))
        self.assertEqual(zero.probability, Decimal("0"))
        self.assertEqual(zero.uncertainty, Decimal("0"))
        self.assertEqual(one.probability, Decimal("1"))
        self.assertEqual(one.uncertainty, Decimal("1"))


if __name__ == "__main__":
    unittest.main()
