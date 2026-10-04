import unittest
from decimal import Decimal

from autosport.calibration_diagnostics import evaluate_calibration_diagnostics
from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
)


class CalibrationOutcomeAvailabilityCutoffTests(unittest.TestCase):
    def _record(
        self,
        forecast_id: str,
        probability: str,
        *,
        generated_at: str,
        input_cutoff: str,
    ) -> ForecastRecord:
        return ForecastRecord(
            quote_key=f"event|winner|{forecast_id}",
            probability=Decimal(probability),
            model_id="calibration-cutoff-falsifier",
            model_version="1.0.0",
            strategy_version="calibration-cutoff-falsifier-v1",
            model_training_cutoff_ts="2026-01-31T23:59:59+00:00",
            input_cutoff_ts=input_cutoff,
            generated_at=generated_at,
            uncertainty=Decimal("0.05"),
            evidence_hashes=("a" * 64,),
            market_snapshot_hash="b" * 64,
            provenance={"dataset": "frozen-holdout"},
            forecast_id=forecast_id,
        )

    def _window(self) -> TemporalEvaluationWindow:
        return TemporalEvaluationWindow(
            "feb-confirmation",
            "2026-01-31T23:59:59+00:00",
            "2026-02-01T00:00:00+00:00",
            "2026-02-28T23:59:59+00:00",
            "holdout",
        )

    def test_outcome_revealed_after_evaluation_cutoff_cannot_be_backfilled(self):
        record = self._record(
            "late-label",
            "0.80",
            generated_at="2026-02-10T12:00:00+00:00",
            input_cutoff="2026-02-10T11:59:00+00:00",
        )
        late_outcome = ForecastOutcomeFact(
            "late-label",
            1,
            "2026-03-01T00:00:00+00:00",
        )

        # The February confirmation view cannot use a label first revealed in
        # March. Accepting it would retroactively improve/alter February
        # calibration evidence with information unavailable at the frozen
        # evaluation cutoff.
        with self.assertRaises(ValueError):
            evaluate_calibration_diagnostics(
                (record,),
                (late_outcome,),
                self._window(),
                bins=2,
            )

    def test_mixed_timely_and_late_labels_keep_confirmation_incomplete(self):
        timely_record = self._record(
            "timely",
            "0.75",
            generated_at="2026-02-10T12:00:00+00:00",
            input_cutoff="2026-02-10T11:59:00+00:00",
        )
        late_record = self._record(
            "late",
            "0.25",
            generated_at="2026-02-11T12:00:00+00:00",
            input_cutoff="2026-02-11T11:59:00+00:00",
        )
        outcomes = (
            ForecastOutcomeFact(
                "timely",
                1,
                "2026-02-10T14:00:00+00:00",
            ),
            ForecastOutcomeFact(
                "late",
                0,
                "2026-03-05T12:00:00+00:00",
            ),
        )

        # A safe evaluator must not silently count the post-cutoff outcome as
        # February truth, nor silently drop that eligible row and call the
        # remaining subset a complete confirmation cohort.
        with self.assertRaises(ValueError):
            evaluate_calibration_diagnostics(
                (timely_record, late_record),
                outcomes,
                self._window(),
                bins=2,
            )


if __name__ == "__main__":
    unittest.main()
