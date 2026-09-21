from __future__ import annotations

import math
from decimal import Decimal

from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
    evaluate_forecast_window,
)


def test_tiny_interior_probability_with_zero_outcome_keeps_positive_log_loss() -> None:
    probability = Decimal("1e-17")
    record = ForecastRecord(
        quote_key="event|winner|tiny-complement",
        probability=probability,
        model_id="logloss-stability-probe",
        model_version="1.0.0",
        strategy_version="logloss-stability-v1",
        model_training_cutoff_ts="2026-01-31T23:59:59+00:00",
        input_cutoff_ts="2026-02-10T11:59:00+00:00",
        generated_at="2026-02-10T12:00:00+00:00",
        uncertainty=Decimal("0"),
        evidence_hashes=("a" * 64,),
        market_snapshot_hash="b" * 64,
        provenance={"dataset": "causal-holdout"},
        forecast_id="tiny-complement",
    )
    outcome = ForecastOutcomeFact(
        "tiny-complement",
        0,
        "2026-02-10T14:00:00+00:00",
    )
    window = TemporalEvaluationWindow(
        "feb-holdout",
        "2026-01-31T23:59:59+00:00",
        "2026-02-01T00:00:00+00:00",
        "2026-02-28T23:59:59+00:00",
        "holdout",
    )

    report = evaluate_forecast_window((record,), (outcome,), window, bins=1)

    expected = -math.log1p(-float(probability))
    assert expected > 0.0
    assert report.log_loss > 0.0
    assert math.isclose(report.log_loss, expected, rel_tol=1e-15, abs_tol=0.0)
