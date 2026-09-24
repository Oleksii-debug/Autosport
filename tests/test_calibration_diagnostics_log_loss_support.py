from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from autosport.calibration_diagnostics import (
    CalibrationPopulationEntry,
    CalibrationPopulationManifest,
    evaluate_calibration_diagnostics,
)
from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
)


_METHOD = "hoeffding-predeclared-probability-support-v2"
_SUPPORT_ERROR = "predeclared log-loss probability support"


def _window() -> TemporalEvaluationWindow:
    return TemporalEvaluationWindow(
        "bounded-log-loss-support",
        "2026-01-31T23:59:59+00:00",
        "2026-02-01T00:00:00+00:00",
        "2026-02-28T23:59:59+00:00",
        "holdout",
    )


def _record(probability: str) -> ForecastRecord:
    return ForecastRecord(
        quote_key="event-support|winner|selection-support",
        probability=Decimal(probability),
        model_id="calibration-baseline",
        model_version="1.0.0",
        strategy_version="calibration-support-v2",
        model_training_cutoff_ts="2026-01-31T23:59:59+00:00",
        input_cutoff_ts="2026-02-10T11:59:00+00:00",
        generated_at="2026-02-10T12:00:00+00:00",
        uncertainty=Decimal("0.05"),
        evidence_hashes=(
            hashlib.sha256(b"evidence:bounded-log-loss-support").hexdigest(),
        ),
        market_snapshot_hash=hashlib.sha256(
            b"snapshot:bounded-log-loss-support"
        ).hexdigest(),
        provenance={"dataset": "causal-holdout"},
        forecast_id="f-support",
    )


def _manifest(record: ForecastRecord) -> CalibrationPopulationManifest:
    return CalibrationPopulationManifest(
        window_id=_window().window_id,
        source_snapshot_sha256="c" * 64,
        selection_policy_sha256="d" * 64,
        entries=(
            CalibrationPopulationEntry(
                forecast_id=record.forecast_id,
                forecast_sha256=record.canonical_hash,
                selected=True,
            ),
        ),
    )


def _evaluate(probability: str, *, outcome: int):
    record = _record(probability)
    return evaluate_calibration_diagnostics(
        (record,),
        (
            ForecastOutcomeFact(
                record.forecast_id,
                outcome,
                "2026-02-10T14:00:00+00:00",
            ),
        ),
        _window(),
        bins=5,
        confidence_level=0.95,
        population_manifest=_manifest(record),
    )


def test_bounded_log_loss_rejects_probability_below_predeclared_support() -> None:
    with pytest.raises(ValueError, match=_SUPPORT_ERROR):
        _evaluate("1e-16", outcome=1)


def test_bounded_log_loss_rejects_probability_above_predeclared_support() -> None:
    # The support is forecast-time and outcome-independent. A probability outside
    # the symmetric support cannot become eligible merely because the realized
    # outcome makes its particular point loss small.
    with pytest.raises(ValueError, match=_SUPPORT_ERROR):
        _evaluate("0.9999999999999999", outcome=1)


def test_support_boundary_keeps_exact_point_score_without_clipping() -> None:
    report = _evaluate("1e-15", outcome=1)

    assert report.log_loss.method == _METHOD
    assert report.log_loss.lower <= report.log_loss.point <= report.log_loss.upper
    assert report.log_loss.point > 0.0
