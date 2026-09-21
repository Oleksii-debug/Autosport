from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Iterable

from .forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
    evaluate_forecast_window,
)

_EPSILON = 1e-15
_BRIER_INTERVAL_METHOD = "hoeffding-bounded-brier-v1"
_LOG_LOSS_INTERVAL_METHOD = "hoeffding-clipped-log-loss-v1"
_CALIBRATION_INTERVAL_METHOD = "bonferroni-wilson-binomial-v1"
_ECE_INTERVAL_METHOD = "simultaneous-bin-envelope-v1"


def _canonical_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("diagnostic values must be finite")
    if value == 0:
        value = 0.0
    return format(value, ".17g")


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MetricUncertainty:
    point: float
    lower: float
    upper: float
    method: str

    def __post_init__(self) -> None:
        for value in (self.point, self.lower, self.upper):
            if type(value) is not float or not math.isfinite(value):
                raise ValueError("metric uncertainty values must be finite floats")
        if not self.method:
            raise ValueError("metric uncertainty method required")
        if self.lower > self.point or self.point > self.upper:
            raise ValueError("metric point must lie inside uncertainty interval")

    def to_payload(self) -> dict[str, str]:
        return {
            "point": _canonical_float(self.point),
            "lower": _canonical_float(self.lower),
            "upper": _canonical_float(self.upper),
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class CalibrationBinUncertainty:
    bin_lower: float
    bin_upper: float
    count: int
    mean_probability: float
    observed_rate: float
    observed_rate_lower: float
    observed_rate_upper: float
    absolute_gap: float
    absolute_gap_lower: float
    absolute_gap_upper: float

    def __post_init__(self) -> None:
        values = (
            self.bin_lower,
            self.bin_upper,
            self.mean_probability,
            self.observed_rate,
            self.observed_rate_lower,
            self.observed_rate_upper,
            self.absolute_gap,
            self.absolute_gap_lower,
            self.absolute_gap_upper,
        )
        if any(type(value) is not float or not math.isfinite(value) for value in values):
            raise ValueError("calibration-bin values must be finite floats")
        if type(self.count) is not int or self.count <= 0:
            raise ValueError("calibration-bin count must be a positive integer")
        if not 0.0 <= self.bin_lower < self.bin_upper <= 1.0:
            raise ValueError("calibration-bin bounds must be inside 0..1")
        for value in (
            self.mean_probability,
            self.observed_rate,
            self.observed_rate_lower,
            self.observed_rate_upper,
            self.absolute_gap,
            self.absolute_gap_lower,
            self.absolute_gap_upper,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("calibration-bin probabilities/gaps must be inside 0..1")
        if not self.observed_rate_lower <= self.observed_rate <= self.observed_rate_upper:
            raise ValueError("observed rate must lie inside Wilson interval")
        if not self.absolute_gap_lower <= self.absolute_gap <= self.absolute_gap_upper:
            raise ValueError("absolute calibration gap must lie inside uncertainty envelope")

    def to_payload(self) -> dict[str, object]:
        return {
            "bin_lower": _canonical_float(self.bin_lower),
            "bin_upper": _canonical_float(self.bin_upper),
            "count": self.count,
            "mean_probability": _canonical_float(self.mean_probability),
            "observed_rate": _canonical_float(self.observed_rate),
            "observed_rate_lower": _canonical_float(self.observed_rate_lower),
            "observed_rate_upper": _canonical_float(self.observed_rate_upper),
            "absolute_gap": _canonical_float(self.absolute_gap),
            "absolute_gap_lower": _canonical_float(self.absolute_gap_lower),
            "absolute_gap_upper": _canonical_float(self.absolute_gap_upper),
        }


@dataclass(frozen=True, slots=True)
class CalibrationDiagnostics:
    window_id: str
    split: str
    count: int
    bins: int
    confidence_level: float
    cohort_sha256: str
    config_sha256: str
    brier_score: MetricUncertainty
    log_loss: MetricUncertainty
    expected_calibration_error: MetricUncertainty
    calibration: tuple[CalibrationBinUncertainty, ...]
    model_versions: tuple[str, ...]
    strategy_versions: tuple[str, ...]
    promotion_authorized: bool = False
    real_money_execution: bool = False

    def __post_init__(self) -> None:
        if not self.window_id or not self.split:
            raise ValueError("window identity required")
        if type(self.count) is not int or self.count <= 0:
            raise ValueError("diagnostic count must be a positive integer")
        if type(self.bins) is not int or self.bins <= 0:
            raise ValueError("bins must be a positive integer")
        if type(self.confidence_level) is not float or not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be a float strictly between 0 and 1")
        for field_name, digest in (
            ("cohort_sha256", self.cohort_sha256),
            ("config_sha256", self.config_sha256),
        ):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{field_name} must be a canonical SHA-256 digest")
        if type(self.calibration) is not tuple or not self.calibration:
            raise ValueError("at least one non-empty calibration bin required")
        if sum(item.count for item in self.calibration) != self.count:
            raise ValueError("calibration bins must cover the complete evaluated cohort")
        if type(self.model_versions) is not tuple or type(self.strategy_versions) is not tuple:
            raise ValueError("version identities must be tuples")
        if self.promotion_authorized is not False or self.real_money_execution is not False:
            raise ValueError("calibration diagnostics cannot authorize promotion or real-money execution")

    @property
    def report_sha256(self) -> str:
        return _digest(self.to_payload(include_identity=False))

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "autosport-calibration-diagnostics",
            "window_id": self.window_id,
            "split": self.split,
            "count": self.count,
            "bins": self.bins,
            "confidence_level": _canonical_float(self.confidence_level),
            "cohort_sha256": self.cohort_sha256,
            "config_sha256": self.config_sha256,
            "brier_score": self.brier_score.to_payload(),
            "log_loss": self.log_loss.to_payload(),
            "expected_calibration_error": self.expected_calibration_error.to_payload(),
            "calibration_interval_method": _CALIBRATION_INTERVAL_METHOD,
            "calibration": [item.to_payload() for item in self.calibration],
            "model_versions": list(self.model_versions),
            "strategy_versions": list(self.strategy_versions),
            "promotion_authorized": False,
            "real_money_execution": False,
        }
        if include_identity:
            payload["report_sha256"] = self.report_sha256
        return payload


def _bounded_mean_interval(
    point: float,
    *,
    count: int,
    lower_bound: float,
    upper_bound: float,
    confidence_level: float,
    method: str,
) -> MetricUncertainty:
    alpha = 1.0 - confidence_level
    half_width = (upper_bound - lower_bound) * math.sqrt(
        math.log(2.0 / alpha) / (2.0 * count)
    )
    return MetricUncertainty(
        point=float(point),
        lower=float(max(lower_bound, point - half_width)),
        upper=float(min(upper_bound, point + half_width)),
        method=method,
    )


def _wilson_interval(
    successes: int,
    count: int,
    *,
    z_value: float,
) -> tuple[float, float]:
    proportion = successes / count
    z_squared = z_value * z_value
    denominator = 1.0 + z_squared / count
    center = (proportion + z_squared / (2.0 * count)) / denominator
    half = (
        z_value
        * math.sqrt(
            proportion * (1.0 - proportion) / count
            + z_squared / (4.0 * count * count)
        )
        / denominator
    )
    return max(0.0, center - half), min(1.0, center + half)


def _distance_to_interval(point: float, lower: float, upper: float) -> float:
    if lower <= point <= upper:
        return 0.0
    return min(abs(point - lower), abs(point - upper))


def evaluate_calibration_diagnostics(
    records: Iterable[ForecastRecord],
    outcomes: Iterable[ForecastOutcomeFact],
    window: TemporalEvaluationWindow,
    *,
    bins: int = 10,
    confidence_level: float = 0.95,
) -> CalibrationDiagnostics:
    """Build bounded calibration uncertainty evidence for one complete temporal cohort.

    Point Brier/log-loss values come from the canonical forecast evaluator.
    Their bands use bounded-loss Hoeffding diagnostics. Per-bin observed-rate
    intervals use a Bonferroni-adjusted Wilson construction so all non-empty
    bin intervals are treated as one simultaneous diagnostic family. The ECE
    envelope is then derived from those bin intervals. These are diagnostics,
    not promotion or execution authority.
    """

    if type(window) is not TemporalEvaluationWindow:
        raise ValueError("window must be an exact TemporalEvaluationWindow")
    if type(bins) is not int or bins <= 0:
        raise ValueError("bins must be a positive integer")
    if (
        type(confidence_level) is not float
        or not math.isfinite(confidence_level)
        or not 0.0 < confidence_level < 1.0
    ):
        raise ValueError("confidence_level must be a finite float strictly between 0 and 1")

    record_values = tuple(records)
    outcome_values = tuple(outcomes)
    if any(type(record) is not ForecastRecord for record in record_values):
        raise ValueError("records must contain exact ForecastRecord values")
    if any(type(fact) is not ForecastOutcomeFact for fact in outcome_values):
        raise ValueError("outcomes must contain exact ForecastOutcomeFact values")

    selected = tuple(
        sorted(
            (record for record in record_values if window.contains(record.generated_at)),
            key=lambda record: record.forecast_id,
        )
    )
    if not selected:
        raise ValueError("no forecasts in evaluation window")

    selected_by_id: dict[str, ForecastRecord] = {}
    for record in selected:
        if record.forecast_id in selected_by_id:
            raise ValueError(f"duplicate forecast_id: {record.forecast_id}")
        selected_by_id[record.forecast_id] = record

    outcome_by_id: dict[str, ForecastOutcomeFact] = {}
    for fact in outcome_values:
        if fact.forecast_id in outcome_by_id:
            raise ValueError(f"duplicate outcome fact for forecast: {fact.forecast_id}")
        outcome_by_id[fact.forecast_id] = fact

    missing = sorted(forecast_id for forecast_id in selected_by_id if forecast_id not in outcome_by_id)
    if missing:
        raise ValueError(
            "complete calibration cohort required; missing outcome facts for: "
            + ", ".join(missing)
        )

    selected_outcomes = tuple(outcome_by_id[record.forecast_id] for record in selected)
    summary = evaluate_forecast_window(selected, selected_outcomes, window, bins=bins)

    paired = tuple(
        sorted(
            (
                (record, outcome_by_id[record.forecast_id])
                for record in selected
            ),
            key=lambda pair: pair[0].forecast_id,
        )
    )
    cohort_payload = [
        {
            "forecast_id": record.forecast_id,
            "forecast_sha256": record.canonical_hash,
            "outcome": fact.outcome,
            "revealed_at": fact.revealed_at,
        }
        for record, fact in paired
    ]
    cohort_sha256 = _digest(cohort_payload)
    config_payload = {
        "schema_version": 1,
        "window": {
            "window_id": window.window_id,
            "training_end_ts": window.training_end_ts,
            "evaluation_start_ts": window.evaluation_start_ts,
            "evaluation_end_ts": window.evaluation_end_ts,
            "split": window.split,
        },
        "bins": bins,
        "confidence_level": _canonical_float(confidence_level),
        "brier_interval_method": _BRIER_INTERVAL_METHOD,
        "log_loss_interval_method": _LOG_LOSS_INTERVAL_METHOD,
        "calibration_interval_method": _CALIBRATION_INTERVAL_METHOD,
        "ece_interval_method": _ECE_INTERVAL_METHOD,
    }
    config_sha256 = _digest(config_payload)

    alpha = 1.0 - confidence_level
    non_empty_bin_count = len(summary.calibration)
    tail_probability = alpha / (2.0 * non_empty_bin_count)
    quantile_probability = 1.0 - tail_probability
    if not 0.5 < quantile_probability < 1.0:
        raise ValueError("confidence_level is too extreme for stable Wilson quantile")
    z_value = NormalDist().inv_cdf(quantile_probability)

    calibration_output: list[CalibrationBinUncertainty] = []
    ece_point = 0.0
    ece_lower = 0.0
    ece_upper = 0.0
    for item in summary.calibration:
        successes = sum(
            fact.outcome
            for record, fact in paired
            if min(bins - 1, int(float(record.probability) * bins))
            == int(round(item.lower * bins))
        )
        lower_rate, upper_rate = _wilson_interval(successes, item.count, z_value=z_value)
        absolute_gap = abs(item.mean_probability - item.observed_rate)
        absolute_gap_lower = _distance_to_interval(
            item.mean_probability, lower_rate, upper_rate
        )
        absolute_gap_upper = max(
            abs(item.mean_probability - lower_rate),
            abs(item.mean_probability - upper_rate),
        )
        weight = item.count / summary.count
        ece_point += weight * absolute_gap
        ece_lower += weight * absolute_gap_lower
        ece_upper += weight * absolute_gap_upper
        calibration_output.append(
            CalibrationBinUncertainty(
                bin_lower=float(item.lower),
                bin_upper=float(item.upper),
                count=item.count,
                mean_probability=float(item.mean_probability),
                observed_rate=float(item.observed_rate),
                observed_rate_lower=float(lower_rate),
                observed_rate_upper=float(upper_rate),
                absolute_gap=float(absolute_gap),
                absolute_gap_lower=float(absolute_gap_lower),
                absolute_gap_upper=float(absolute_gap_upper),
            )
        )

    brier = _bounded_mean_interval(
        summary.brier_score,
        count=summary.count,
        lower_bound=0.0,
        upper_bound=1.0,
        confidence_level=confidence_level,
        method=_BRIER_INTERVAL_METHOD,
    )
    max_log_loss = -math.log(_EPSILON)
    log_loss = _bounded_mean_interval(
        summary.log_loss,
        count=summary.count,
        lower_bound=0.0,
        upper_bound=max_log_loss,
        confidence_level=confidence_level,
        method=_LOG_LOSS_INTERVAL_METHOD,
    )
    ece = MetricUncertainty(
        point=float(ece_point),
        lower=float(max(0.0, ece_lower)),
        upper=float(min(1.0, ece_upper)),
        method=_ECE_INTERVAL_METHOD,
    )

    return CalibrationDiagnostics(
        window_id=summary.window_id,
        split=summary.split,
        count=summary.count,
        bins=bins,
        confidence_level=confidence_level,
        cohort_sha256=cohort_sha256,
        config_sha256=config_sha256,
        brier_score=brier,
        log_loss=log_loss,
        expected_calibration_error=ece,
        calibration=tuple(calibration_output),
        model_versions=summary.model_versions,
        strategy_versions=summary.strategy_versions,
    )
