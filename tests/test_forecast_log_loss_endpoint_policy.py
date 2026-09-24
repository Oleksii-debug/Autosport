from __future__ import annotations

import math
import unittest
from decimal import Decimal, localcontext

from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
    evaluate_forecast_window,
)


class ForecastLogLossEndpointPolicyTests(unittest.TestCase):
    def _record(
        self,
        forecast_id: str,
        probability: str,
        *,
        generated_at: str = "2026-02-10T12:00:00+00:00",
        input_cutoff: str = "2026-02-10T11:59:00+00:00",
    ) -> ForecastRecord:
        return ForecastRecord(
            quote_key=f"event|winner|{forecast_id}",
            probability=Decimal(probability),
            model_id="endpoint-policy-probe",
            model_version="1.0.0",
            strategy_version="endpoint-policy-v1",
            model_training_cutoff_ts="2026-01-31T23:59:59+00:00",
            input_cutoff_ts=input_cutoff,
            generated_at=generated_at,
            uncertainty=Decimal("0"),
            evidence_hashes=("a" * 64,),
            market_snapshot_hash="b" * 64,
            provenance={"dataset": "causal-holdout"},
            forecast_id=forecast_id,
        )

    def _window(self) -> TemporalEvaluationWindow:
        return TemporalEvaluationWindow(
            "feb-holdout",
            "2026-01-31T23:59:59+00:00",
            "2026-02-01T00:00:00+00:00",
            "2026-02-28T23:59:59+00:00",
            "holdout",
        )

    def _evaluate_one(self, probability: str, outcome: int):
        record = self._record("f", probability)
        fact = ForecastOutcomeFact(
            "f",
            outcome,
            "2026-02-10T14:00:00+00:00",
        )
        return evaluate_forecast_window((record,), (fact,), self._window(), bins=1)

    def test_probability_zero_with_realized_one_is_unbounded_not_clipped(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "log loss is unbounded for probability=0 and realized outcome=1",
        ):
            self._evaluate_one("0", 1)

    def test_probability_one_with_realized_zero_is_unbounded_not_clipped(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "log loss is unbounded for probability=1 and realized outcome=0",
        ):
            self._evaluate_one("1", 0)

    def test_correct_declared_endpoints_have_zero_log_loss(self) -> None:
        records = (
            self._record("f-zero", "0"),
            self._record(
                "f-one",
                "1",
                generated_at="2026-02-11T12:00:00+00:00",
                input_cutoff="2026-02-11T11:59:00+00:00",
            ),
        )
        outcomes = (
            ForecastOutcomeFact(
                "f-zero",
                0,
                "2026-02-10T14:00:00+00:00",
            ),
            ForecastOutcomeFact(
                "f-one",
                1,
                "2026-02-11T14:00:00+00:00",
            ),
        )

        report = evaluate_forecast_window(
            records,
            outcomes,
            self._window(),
            bins=2,
        )

        self.assertEqual(report.log_loss, 0.0)
        self.assertEqual(report.brier_score, 0.0)

    def test_interior_probability_below_old_epsilon_is_not_silently_clipped(self) -> None:
        report = self._evaluate_one("1e-16", 1)

        expected = -math.log(1e-16)
        old_clipped = -math.log(1e-15)
        self.assertAlmostEqual(report.log_loss, expected)
        self.assertGreater(report.log_loss, old_clipped)

    def test_ordinary_interior_probability_preserves_binary_log_loss_formula(self) -> None:
        report = self._evaluate_one("0.2", 0)

        self.assertAlmostEqual(report.log_loss, -math.log1p(-0.2))

    def test_tiny_interior_probability_with_zero_outcome_keeps_positive_log_loss(self) -> None:
        probability = 1e-17
        report = self._evaluate_one("1e-17", 0)

        expected = -math.log1p(-probability)
        self.assertGreater(expected, 0.0)
        self.assertGreater(report.log_loss, 0.0)
        self.assertTrue(
            math.isclose(report.log_loss, expected, rel_tol=1e-15, abs_tol=0.0)
        )

    def test_near_one_probability_uses_declared_decimal_complement(self) -> None:
        probability = Decimal("0.9999999999999999")
        report = self._evaluate_one(str(probability), 0)

        expected = float(-Decimal("1e-16").ln())
        binary64_quantized = -math.log1p(-float(probability))
        self.assertAlmostEqual(report.log_loss, expected)
        self.assertGreater(abs(report.log_loss - binary64_quantized), 0.01)

    def test_extreme_interior_decimals_are_scored_before_float_conversion(self) -> None:
        near_one = self._evaluate_one(
            "0.999999999999999999999999999999",
            0,
        )
        tiny = self._evaluate_one("1e-1000", 1)
        compact_extreme = self._evaluate_one("1e-1000000", 1)

        self.assertAlmostEqual(
            near_one.log_loss,
            float(-Decimal("1e-30").ln()),
        )
        self.assertAlmostEqual(
            tiny.log_loss,
            float(-Decimal("1e-1000").ln()),
        )
        self.assertAlmostEqual(
            compact_extreme.log_loss,
            float(-Decimal("1e-1000000").ln()),
        )

    def test_log_loss_is_independent_of_ambient_decimal_precision(self) -> None:
        probability = "0.999999999999999999999999999999"
        with localcontext() as context:
            context.prec = 6
            low_precision = self._evaluate_one(probability, 0).log_loss
        with localcontext() as context:
            context.prec = 50
            high_precision = self._evaluate_one(probability, 0).log_loss

        self.assertEqual(low_precision, high_precision)
        self.assertAlmostEqual(low_precision, float(-Decimal("1e-30").ln()))

    def test_positive_loss_that_underflows_summary_float_fails_closed(self) -> None:
        for probability in ("1e-1000", "1e-1000000"):
            with self.subTest(probability=probability):
                with self.assertRaisesRegex(
                    ValueError,
                    "positive log loss is not representable as a nonzero binary64",
                ):
                    self._evaluate_one(probability, 0)


if __name__ == "__main__":
    unittest.main()
