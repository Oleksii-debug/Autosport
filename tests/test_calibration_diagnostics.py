import unittest
from decimal import Decimal

from autosport.calibration_diagnostics import evaluate_calibration_diagnostics
from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
    evaluate_forecast_window,
)


class CalibrationDiagnosticsTests(unittest.TestCase):
    def _record(
        self,
        forecast_id: str,
        probability: str,
        *,
        generated_at: str = "2026-02-10T12:00:00+00:00",
        input_cutoff: str = "2026-02-10T11:59:00+00:00",
        training_cutoff: str = "2026-01-31T23:59:59+00:00",
    ) -> ForecastRecord:
        return ForecastRecord(
            quote_key=f"event|winner|{forecast_id}",
            probability=Decimal(probability),
            model_id="calibration-baseline",
            model_version="1.0.0",
            strategy_version="calibration-diagnostics-v1",
            model_training_cutoff_ts=training_cutoff,
            input_cutoff_ts=input_cutoff,
            generated_at=generated_at,
            uncertainty=Decimal("0.05"),
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

    def _cohort(self):
        records = (
            self._record("f-1", "0.90"),
            self._record(
                "f-2",
                "0.80",
                generated_at="2026-02-11T12:00:00+00:00",
                input_cutoff="2026-02-11T11:59:00+00:00",
            ),
            self._record(
                "f-3",
                "0.20",
                generated_at="2026-02-12T12:00:00+00:00",
                input_cutoff="2026-02-12T11:59:00+00:00",
            ),
            self._record(
                "f-4",
                "0.10",
                generated_at="2026-02-13T12:00:00+00:00",
                input_cutoff="2026-02-13T11:59:00+00:00",
            ),
        )
        outcomes = (
            ForecastOutcomeFact("f-1", 1, "2026-02-10T14:00:00+00:00"),
            ForecastOutcomeFact("f-2", 1, "2026-02-11T14:00:00+00:00"),
            ForecastOutcomeFact("f-3", 0, "2026-02-12T14:00:00+00:00"),
            ForecastOutcomeFact("f-4", 0, "2026-02-13T14:00:00+00:00"),
        )
        return records, outcomes

    def test_preserves_canonical_point_metrics_and_adds_bounded_uncertainty(self):
        records, outcomes = self._cohort()
        window = self._window()
        base = evaluate_forecast_window(records, outcomes, window, bins=5)
        report = evaluate_calibration_diagnostics(
            records,
            outcomes,
            window,
            bins=5,
            confidence_level=0.95,
        )

        self.assertEqual(report.count, base.count)
        self.assertEqual(report.brier_score.point, base.brier_score)
        self.assertEqual(report.log_loss.point, base.log_loss)
        self.assertLessEqual(report.brier_score.lower, report.brier_score.point)
        self.assertGreaterEqual(report.brier_score.upper, report.brier_score.point)
        self.assertLessEqual(report.log_loss.lower, report.log_loss.point)
        self.assertGreaterEqual(report.log_loss.upper, report.log_loss.point)
        self.assertEqual(sum(item.count for item in report.calibration), report.count)
        self.assertTrue(
            all(
                item.observed_rate_lower
                <= item.observed_rate
                <= item.observed_rate_upper
                for item in report.calibration
            )
        )
        self.assertLessEqual(
            report.expected_calibration_error.lower,
            report.expected_calibration_error.point,
        )
        self.assertGreaterEqual(
            report.expected_calibration_error.upper,
            report.expected_calibration_error.point,
        )
        self.assertFalse(report.promotion_authorized)
        self.assertFalse(report.real_money_execution)
        self.assertEqual(len(report.report_sha256), 64)

    def test_complete_cohort_is_required(self):
        records, outcomes = self._cohort()
        with self.assertRaisesRegex(ValueError, "complete calibration cohort required"):
            evaluate_calibration_diagnostics(
                records,
                outcomes[:-1],
                self._window(),
                bins=5,
            )

    def test_input_order_does_not_change_evidence_identity(self):
        records, outcomes = self._cohort()
        forward = evaluate_calibration_diagnostics(
            records,
            outcomes,
            self._window(),
            bins=5,
        )
        reverse = evaluate_calibration_diagnostics(
            tuple(reversed(records)),
            tuple(reversed(outcomes)),
            self._window(),
            bins=5,
        )
        self.assertEqual(forward.cohort_sha256, reverse.cohort_sha256)
        self.assertEqual(forward.config_sha256, reverse.config_sha256)
        self.assertEqual(forward.report_sha256, reverse.report_sha256)

    def test_outcome_change_changes_cohort_and_report_identity(self):
        records, outcomes = self._cohort()
        original = evaluate_calibration_diagnostics(
            records,
            outcomes,
            self._window(),
            bins=5,
        )
        changed_outcomes = outcomes[:-1] + (
            ForecastOutcomeFact("f-4", 1, "2026-02-13T14:00:00+00:00"),
        )
        changed = evaluate_calibration_diagnostics(
            records,
            changed_outcomes,
            self._window(),
            bins=5,
        )
        self.assertNotEqual(original.cohort_sha256, changed.cohort_sha256)
        self.assertNotEqual(original.report_sha256, changed.report_sha256)

    def test_duplicate_outcome_and_bad_config_fail_closed(self):
        records, outcomes = self._cohort()
        duplicate = outcomes + (outcomes[0],)
        with self.assertRaisesRegex(ValueError, "duplicate outcome"):
            evaluate_calibration_diagnostics(
                records,
                duplicate,
                self._window(),
                bins=5,
            )
        with self.assertRaisesRegex(ValueError, "bins must be a positive integer"):
            evaluate_calibration_diagnostics(
                records,
                outcomes,
                self._window(),
                bins=True,
            )
        with self.assertRaisesRegex(ValueError, "confidence_level"):
            evaluate_calibration_diagnostics(
                records,
                outcomes,
                self._window(),
                confidence_level=1,
            )

    def test_causal_and_training_boundary_checks_are_preserved(self):
        records, outcomes = self._cohort()
        leaked = (
            self._record(
                "f-1",
                "0.90",
                training_cutoff="2026-02-02T00:00:00+00:00",
            ),
        ) + records[1:]
        with self.assertRaisesRegex(ValueError, "holdout/walk-forward leakage"):
            evaluate_calibration_diagnostics(
                leaked,
                outcomes,
                self._window(),
                bins=5,
            )

        early_reveal = (
            ForecastOutcomeFact("f-1", 1, "2026-02-10T11:00:00+00:00"),
        ) + outcomes[1:]
        with self.assertRaisesRegex(ValueError, "outcome reveal must be after forecast generation"):
            evaluate_calibration_diagnostics(
                records,
                early_reveal,
                self._window(),
                bins=5,
            )


if __name__ == "__main__":
    unittest.main()
