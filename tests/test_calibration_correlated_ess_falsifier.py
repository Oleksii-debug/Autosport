from __future__ import annotations

import unittest
from decimal import Decimal

from autosport.calibration_diagnostics import evaluate_calibration_diagnostics
from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
)


class CalibrationCorrelatedEffectiveSampleSizeFalsifierTests(unittest.TestCase):
    """Correlated rows must not create IID uncertainty evidence by repetition."""

    def _window(self) -> TemporalEvaluationWindow:
        return TemporalEvaluationWindow(
            "feb-correlated-holdout",
            "2026-01-31T23:59:59+00:00",
            "2026-02-01T00:00:00+00:00",
            "2026-02-28T23:59:59+00:00",
            "holdout",
        )

    def _record(self, index: int) -> ForecastRecord:
        day = 10 + index
        generated_at = f"2026-02-{day:02d}T12:00:00+00:00"
        input_cutoff = f"2026-02-{day:02d}T11:59:00+00:00"
        return ForecastRecord(
            quote_key="shared-event|winner|selection-1",
            probability=Decimal("0.70"),
            model_id="correlated-calibration-model",
            model_version="1.0.0",
            strategy_version="correlated-calibration-v1",
            model_training_cutoff_ts="2026-01-31T23:59:59+00:00",
            input_cutoff_ts=input_cutoff,
            generated_at=generated_at,
            uncertainty=Decimal("0.05"),
            evidence_hashes=("a" * 64,),
            market_snapshot_hash="b" * 64,
            provenance={
                "dataset": "causal-holdout",
                "dependence_note": "same underlying event/selection cluster",
            },
            forecast_id=f"correlated-{index}",
        )

    def _outcome(self, index: int) -> ForecastOutcomeFact:
        day = 10 + index
        return ForecastOutcomeFact(
            f"correlated-{index}",
            1,
            f"2026-02-{day:02d}T14:00:00+00:00",
        )

    @staticmethod
    def _metric_width(metric: object) -> float:
        return float(metric.upper) - float(metric.lower)

    @staticmethod
    def _rate_width(report: object) -> float:
        item = report.calibration[0]
        return float(item.observed_rate_upper) - float(item.observed_rate_lower)

    def test_repeating_one_correlation_cluster_cannot_shrink_uncertainty_as_iid(self):
        single_records = (self._record(0),)
        single_outcomes = (self._outcome(0),)
        single = evaluate_calibration_diagnostics(
            single_records,
            single_outcomes,
            self._window(),
            bins=1,
            confidence_level=0.95,
        )

        repeated_records = tuple(self._record(index) for index in range(12))
        repeated_outcomes = tuple(self._outcome(index) for index in range(12))

        try:
            repeated = evaluate_calibration_diagnostics(
                repeated_records,
                repeated_outcomes,
                self._window(),
                bins=1,
                confidence_level=0.95,
            )
        except ValueError:
            # Fail-closed is valid until an explicit, predeclared dependence/ESS
            # contract tells the evaluator how many independent units exist.
            return

        self.assertGreaterEqual(
            self._metric_width(repeated.brier_score),
            self._metric_width(single.brier_score),
            "raw correlated row count must not narrow Brier uncertainty as IID N",
        )
        self.assertGreaterEqual(
            self._metric_width(repeated.log_loss),
            self._metric_width(single.log_loss),
            "raw correlated row count must not narrow log-loss uncertainty as IID N",
        )
        self.assertGreaterEqual(
            self._rate_width(repeated),
            self._rate_width(single),
            "raw correlated row count must not narrow calibration-rate uncertainty",
        )


if __name__ == "__main__":
    unittest.main()
