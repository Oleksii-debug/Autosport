from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
    evaluate_walk_forward,
)


@dataclass(frozen=True, slots=True)
class WalkForwardBundle:
    forecasts: tuple[ForecastRecord, ...]
    outcomes: tuple[ForecastOutcomeFact, ...]
    windows: tuple[TemporalEvaluationWindow, ...]
    bins: int
    source_sha256: str

    @classmethod
    def from_path(cls, path: str | Path) -> "WalkForwardBundle":
        raw_bytes = Path(path).read_bytes()
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("walk-forward bundle must be valid UTF-8 JSON") from exc
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls.from_dict(raw, source_sha256=digest)

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        source_sha256: str | None = None,
    ) -> "WalkForwardBundle":
        if not isinstance(raw, dict):
            raise ValueError("walk-forward bundle root must be an object")
        if raw.get("schema_version") != 1:
            raise ValueError("walk-forward bundle schema_version must be 1")

        forecast_values = raw.get("forecasts")
        outcome_values = raw.get("outcomes")
        window_values = raw.get("windows")
        if not isinstance(forecast_values, list) or not forecast_values:
            raise ValueError("walk-forward bundle forecasts must be a non-empty list")
        if not isinstance(outcome_values, list) or not outcome_values:
            raise ValueError("walk-forward bundle outcomes must be a non-empty list")
        if not isinstance(window_values, list) or not window_values:
            raise ValueError("walk-forward bundle windows must be a non-empty list")

        forecasts = tuple(_forecast_from_dict(item) for item in forecast_values)
        outcomes = tuple(_outcome_from_dict(item) for item in outcome_values)
        windows = tuple(_window_from_dict(item) for item in window_values)
        bins = int(raw.get("bins", 10))
        if bins <= 0:
            raise ValueError("walk-forward bundle bins must be positive")

        _validate_global_identity(forecasts, outcomes, windows)
        if source_sha256 is None:
            canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            source_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(forecasts, outcomes, windows, bins, source_sha256.lower())


def evaluate_walk_forward_bundle(bundle: WalkForwardBundle) -> dict[str, Any]:
    """Strict product wrapper around temporal evaluation.

    Every forecast that falls inside an evaluation window must have exactly one
    outcome fact. Silently dropping unsettled forecasts would bias holdout
    metrics, so incomplete cohorts fail closed instead of shrinking the sample.
    """

    outcome_by_id = {fact.forecast_id: fact for fact in bundle.outcomes}
    evaluated_ids: set[str] = set()
    missing_outcomes: list[str] = []

    for record in bundle.forecasts:
        matching = [window for window in bundle.windows if window.contains(record.generated_at)]
        if len(matching) > 1:
            # evaluate_walk_forward also rejects overlapping windows, but keep the
            # cohort completeness check independently fail-closed.
            raise ValueError(f"forecast belongs to multiple evaluation windows: {record.forecast_id}")
        if not matching:
            continue
        evaluated_ids.add(record.forecast_id)
        if record.forecast_id not in outcome_by_id:
            missing_outcomes.append(record.forecast_id)

    if missing_outcomes:
        raise ValueError(
            "walk-forward evaluation cohort is incomplete; missing outcome facts for: "
            + ",".join(sorted(missing_outcomes))
        )

    summaries = evaluate_walk_forward(
        bundle.forecasts,
        bundle.outcomes,
        bundle.windows,
        bins=bundle.bins,
    )
    evaluated_from_summaries = sum(summary.count for summary in summaries)
    if evaluated_from_summaries != len(evaluated_ids):
        raise ValueError("walk-forward evaluator did not account for the complete evaluation cohort")

    return {
        "schema_version": 1,
        "kind": "strict_walk_forward_forecast_evaluation",
        "source_sha256": bundle.source_sha256,
        "evaluation_mode": "complete-cohort-causal-walk-forward",
        "input_forecast_count": len(bundle.forecasts),
        "input_outcome_count": len(bundle.outcomes),
        "evaluated_forecast_count": evaluated_from_summaries,
        "window_count": len(summaries),
        "bins": bundle.bins,
        "windows": [_summary_payload(summary) for summary in summaries],
        "profitability_claim": False,
        "real_money_execution": False,
    }


def _validate_global_identity(
    forecasts: tuple[ForecastRecord, ...],
    outcomes: tuple[ForecastOutcomeFact, ...],
    windows: tuple[TemporalEvaluationWindow, ...],
) -> None:
    forecast_ids = [record.forecast_id for record in forecasts]
    if len(forecast_ids) != len(set(forecast_ids)):
        raise ValueError("walk-forward bundle contains duplicate forecast_id")
    outcome_ids = [fact.forecast_id for fact in outcomes]
    if len(outcome_ids) != len(set(outcome_ids)):
        raise ValueError("walk-forward bundle contains duplicate outcome fact")
    unknown_outcomes = sorted(set(outcome_ids).difference(forecast_ids))
    if unknown_outcomes:
        raise ValueError(
            "walk-forward bundle contains outcome facts for unknown forecasts: "
            + ",".join(unknown_outcomes)
        )
    window_ids = [window.window_id for window in windows]
    if len(window_ids) != len(set(window_ids)):
        raise ValueError("walk-forward bundle contains duplicate window_id")


def _forecast_from_dict(raw: Any) -> ForecastRecord:
    if not isinstance(raw, dict):
        raise ValueError("walk-forward forecast entry must be an object")
    return ForecastRecord(
        quote_key=str(raw["quote_key"]),
        probability=raw["probability"],
        model_id=str(raw["model_id"]),
        model_version=str(raw["model_version"]),
        strategy_version=str(raw["strategy_version"]),
        model_training_cutoff_ts=str(raw["model_training_cutoff_ts"]),
        input_cutoff_ts=str(raw["input_cutoff_ts"]),
        generated_at=str(raw["generated_at"]),
        uncertainty=raw.get("uncertainty", "0"),
        evidence_hashes=tuple(str(item) for item in raw.get("evidence_hashes", ())),
        market_snapshot_hash=(
            str(raw["market_snapshot_hash"])
            if raw.get("market_snapshot_hash") is not None
            else None
        ),
        provenance=dict(raw.get("provenance", {})),
        forecast_id=str(raw["forecast_id"]),
    )


def _outcome_from_dict(raw: Any) -> ForecastOutcomeFact:
    if not isinstance(raw, dict):
        raise ValueError("walk-forward outcome entry must be an object")
    return ForecastOutcomeFact(
        forecast_id=str(raw["forecast_id"]),
        outcome=int(raw["outcome"]),
        revealed_at=str(raw["revealed_at"]),
    )


def _window_from_dict(raw: Any) -> TemporalEvaluationWindow:
    if not isinstance(raw, dict):
        raise ValueError("walk-forward window entry must be an object")
    return TemporalEvaluationWindow(
        window_id=str(raw["window_id"]),
        training_end_ts=str(raw["training_end_ts"]),
        evaluation_start_ts=str(raw["evaluation_start_ts"]),
        evaluation_end_ts=str(raw["evaluation_end_ts"]),
        split=str(raw.get("split", "holdout")),
    )


def _summary_payload(summary: Any) -> dict[str, Any]:
    return {
        "window_id": summary.window_id,
        "split": summary.split,
        "count": summary.count,
        "brier_score": summary.brier_score,
        "log_loss": summary.log_loss,
        "mean_uncertainty": summary.mean_uncertainty,
        "model_versions": list(summary.model_versions),
        "strategy_versions": list(summary.strategy_versions),
        "calibration": [asdict(item) for item in summary.calibration],
    }
