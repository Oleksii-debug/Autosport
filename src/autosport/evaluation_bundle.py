from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .dataset import ReplayDataset, load_dataset
from .forecast_origin import (
    ForecastOriginBinding,
    load_forecast_origin_binding,
    verify_forecast_origin_binding,
)
from .forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
    evaluate_walk_forward,
    parse_iso_timestamp,
)


@dataclass(frozen=True, slots=True)
class WalkForwardBundle:
    forecasts: tuple[ForecastRecord, ...]
    outcomes: tuple[ForecastOutcomeFact, ...]
    windows: tuple[TemporalEvaluationWindow, ...]
    bins: int
    source_sha256: str
    governed_dataset: ReplayDataset | None = None
    forecast_origin_binding: ForecastOriginBinding | None = None
    bundle_schema_version: int = 1

    @classmethod
    def from_path(cls, path: str | Path) -> "WalkForwardBundle":
        source = Path(path)
        raw_bytes = source.read_bytes()
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("walk-forward bundle must be valid UTF-8 JSON") from exc
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        governed_dataset = None
        forecast_origin_binding = None
        if isinstance(raw, dict) and raw.get("schema_version") == 2:
            governed_dataset = _load_declared_governed_dataset(raw, source)
            if raw.get("forecast_origin") is not None:
                forecast_origin_binding = load_forecast_origin_binding(raw["forecast_origin"], source)
        return cls.from_dict(
            raw,
            source_sha256=digest,
            governed_dataset=governed_dataset,
            forecast_origin_binding=forecast_origin_binding,
        )

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        source_sha256: str | None = None,
        governed_dataset: ReplayDataset | None = None,
        forecast_origin_binding: ForecastOriginBinding | None = None,
    ) -> "WalkForwardBundle":
        if not isinstance(raw, dict):
            raise ValueError("walk-forward bundle root must be an object")
        schema_version = raw.get("schema_version")
        if schema_version not in {1, 2}:
            raise ValueError("walk-forward bundle schema_version must be 1 or 2")
        if schema_version == 1:
            if raw.get("dataset") is not None:
                raise ValueError("walk-forward schema_version 1 must not declare governed dataset identity")
            if raw.get("forecast_origin") is not None:
                raise ValueError("walk-forward schema_version 1 must not declare forecast origin evidence")
        if schema_version == 2:
            declaration = raw.get("dataset")
            if not isinstance(declaration, dict):
                raise ValueError("walk-forward schema_version 2 requires dataset object")
            if governed_dataset is None:
                raise ValueError(
                    "walk-forward schema_version 2 requires verified governed dataset context"
                )
            _validate_declared_dataset_identity(declaration, governed_dataset)
            if raw.get("forecast_origin") is not None and forecast_origin_binding is None:
                raise ValueError(
                    "walk-forward forecast_origin declaration requires verified local origin artifacts"
                )

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
        return cls(
            forecasts,
            outcomes,
            windows,
            bins,
            source_sha256.lower(),
            governed_dataset=governed_dataset,
            forecast_origin_binding=forecast_origin_binding,
            bundle_schema_version=int(schema_version),
        )


def evaluate_walk_forward_bundle(bundle: WalkForwardBundle) -> dict[str, Any]:
    """Strict product wrapper around temporal evaluation.

    Every forecast that falls inside an evaluation window must have exactly one
    outcome fact. Silently dropping unsettled forecasts would bias holdout
    metrics, so incomplete cohorts fail closed instead of shrinking the sample.

    Schema-v2 bundles additionally bind the cohort to a locally verified,
    governed historical ReplayDataset. Optional forecast-origin evidence can
    further bind each evaluated ForecastRecord to a canonical pre-outcome
    research decision ledger prefix committed by a matching durable run summary.
    """

    outcome_by_id = {fact.forecast_id: fact for fact in bundle.outcomes}
    evaluated_ids: set[str] = set()
    missing_outcomes: list[str] = []

    for record in bundle.forecasts:
        matching = [window for window in bundle.windows if window.contains(record.generated_at)]
        if len(matching) > 1:
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

    governed_evidence = _validate_governed_dataset_cohort(bundle)
    origin_evidence = None
    if bundle.forecast_origin_binding is not None:
        if bundle.governed_dataset is None:
            raise ValueError("forecast origin proof requires governed historical dataset binding")
        origin_evidence = verify_forecast_origin_binding(
            bundle.forecast_origin_binding,
            bundle.governed_dataset,
            bundle.forecasts,
            evaluated_ids,
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

    governed = governed_evidence is not None
    origin_verified = origin_evidence is not None
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "strict_walk_forward_forecast_evaluation",
        "source_sha256": bundle.source_sha256,
        "input_bundle_schema_version": bundle.bundle_schema_version,
        "evaluation_mode": (
            "governed-historical-preoutcome-origin-complete-cohort-causal-walk-forward"
            if origin_verified
            else (
                "governed-historical-complete-cohort-causal-walk-forward"
                if governed
                else "complete-cohort-causal-walk-forward"
            )
        ),
        "input_forecast_count": len(bundle.forecasts),
        "input_outcome_count": len(bundle.outcomes),
        "evaluated_forecast_count": evaluated_from_summaries,
        "window_count": len(summaries),
        "bins": bundle.bins,
        "windows": [_summary_payload(summary) for summary in summaries],
        "truth": {
            "governed_historical_import": governed,
            "sealed_dataset_identity_verified": governed,
            "sealed_outcomes_bound_to_forecasts": governed,
            "outcome_reveal_boundary_verified": governed,
            "temporal_timestamp_constraints_verified": governed,
            "canonical_forecast_origin_verified": origin_verified,
            "pre_outcome_ledger_write_verified": origin_verified,
            "temporal_holdout_protocol_verified": origin_verified,
            "historical_window_market_coverage_verified": False,
            "licensing_retention_verified": False,
            "profitability_claim": False,
            "predictive_superiority_claim": False,
            "real_money_execution": False,
        },
        "profitability_claim": False,
        "real_money_execution": False,
    }
    if governed_evidence is not None:
        report["dataset"] = governed_evidence
    if origin_evidence is not None:
        report["forecast_origin"] = origin_evidence
    return report


def _load_declared_governed_dataset(raw: dict[str, Any], source: Path) -> ReplayDataset:
    declaration = raw.get("dataset")
    if not isinstance(declaration, dict):
        raise ValueError("walk-forward schema_version 2 requires dataset object")
    path_value = declaration.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError("walk-forward dataset.path must be a non-empty relative path")
    relative = Path(path_value.strip())
    if relative.is_absolute():
        raise ValueError("walk-forward dataset.path must be relative to the bundle")
    parent = source.parent.resolve()
    candidate = (parent / relative).resolve()
    try:
        candidate.relative_to(parent)
    except ValueError as exc:
        raise ValueError("walk-forward dataset.path escapes the bundle directory") from exc
    dataset = load_dataset(candidate)
    _validate_declared_dataset_identity(declaration, dataset)
    return dataset


def _validate_declared_dataset_identity(
    declaration: dict[str, Any],
    dataset: ReplayDataset,
) -> None:
    if dataset.schema_version != 2 or dataset.governance is None or dataset.import_identity is None:
        raise ValueError("walk-forward schema_version 2 requires a governed historical schema-v2 dataset")
    expected_import = _required_sha256(
        declaration,
        "historical_import_identity",
        context="walk-forward dataset",
    )
    expected_market = _required_sha256(
        declaration,
        "market_sha256",
        context="walk-forward dataset",
    )
    expected_results = _required_sha256(
        declaration,
        "sealed_results_sha256",
        context="walk-forward dataset",
    )
    if expected_import != dataset.import_identity:
        raise ValueError("walk-forward historical_import_identity does not match verified dataset")
    if expected_market != dataset.market_sha256:
        raise ValueError("walk-forward market_sha256 does not match verified dataset")
    if expected_results != dataset.results_sha256:
        raise ValueError("walk-forward sealed_results_sha256 does not match verified dataset")


def _validate_governed_dataset_cohort(bundle: WalkForwardBundle) -> dict[str, Any] | None:
    dataset = bundle.governed_dataset
    if dataset is None:
        return None
    governance = dataset.governance
    if governance is None or dataset.import_identity is None:
        raise ValueError("governed walk-forward evaluation requires historical dataset governance")

    events = dataset.load_market_events()
    first_observed_by_quote: dict[str, Any] = {}
    for event in events:
        observed = parse_iso_timestamp(event.observed_ts)
        current = first_observed_by_quote.get(event.quote_key)
        if current is None or observed < current:
            first_observed_by_quote[event.quote_key] = observed
    sealed_outcomes = dataset.load_results_after_replay()
    coverage_start = parse_iso_timestamp(governance.coverage_start_ts)
    coverage_end = parse_iso_timestamp(governance.coverage_end_ts)
    reveal_after = parse_iso_timestamp(governance.outcome_reveal_after)

    forecast_by_id = {record.forecast_id: record for record in bundle.forecasts}
    for record in bundle.forecasts:
        first_observed = first_observed_by_quote.get(record.quote_key)
        if first_observed is None:
            raise ValueError(
                f"walk-forward forecast quote_key is absent from governed historical corpus: {record.quote_key}"
            )
        input_cutoff = parse_iso_timestamp(record.input_cutoff_ts)
        generated = parse_iso_timestamp(record.generated_at)
        if input_cutoff < coverage_start or input_cutoff > coverage_end:
            raise ValueError("walk-forward forecast input cutoff is outside governed dataset coverage")
        if generated < coverage_start or generated > coverage_end:
            raise ValueError("walk-forward forecast generation is outside governed dataset coverage")
        if input_cutoff < first_observed:
            raise ValueError(
                "walk-forward forecast input cutoff predates the quote's first governed observation"
            )
        if generated < first_observed:
            raise ValueError("walk-forward forecast was generated before its quote existed in governed corpus")

    for fact in bundle.outcomes:
        record = forecast_by_id[fact.forecast_id]
        sealed = sealed_outcomes.get(record.quote_key)
        if sealed is None:
            raise ValueError(
                f"governed sealed results lack forecast quote_key: {record.quote_key}"
            )
        if sealed == "void":
            raise ValueError(
                "binary walk-forward metrics cannot score a void sealed outcome; cohort must exclude it explicitly"
            )
        expected = 1 if sealed == "win" else 0
        if fact.outcome != expected:
            raise ValueError(
                f"walk-forward outcome fact disagrees with governed sealed outcome: {fact.forecast_id}"
            )
        if parse_iso_timestamp(fact.revealed_at) != reveal_after:
            raise ValueError(
                "walk-forward outcome reveal timestamp must equal governed sealed-results reveal boundary"
            )

    for window in bundle.windows:
        evaluation_start = parse_iso_timestamp(window.evaluation_start_ts)
        evaluation_end = parse_iso_timestamp(window.evaluation_end_ts)
        if evaluation_start < coverage_start or evaluation_end > coverage_end:
            raise ValueError("walk-forward evaluation window is outside governed dataset coverage")

    return {
        "name": dataset.name,
        "sport": dataset.sport,
        "dataset_schema_version": dataset.schema_version,
        "market_sha256": dataset.market_sha256,
        "sealed_results_sha256": dataset.results_sha256,
        "historical_import_identity": dataset.import_identity,
        "source_identity": governance.source_identity,
        "coverage_start_ts": governance.coverage_start_ts,
        "coverage_end_ts": governance.coverage_end_ts,
        "outcome_reveal_after": governance.outcome_reveal_after,
        "source_ids": list(governance.source_ids),
        "market_types": list(governance.market_types),
        "redistribution_policy": governance.redistribution_policy,
    }


def _required_sha256(
    raw: dict[str, Any],
    key: str,
    *,
    context: str,
) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{context}.{key} must be a SHA-256 hex digest")
    lowered = value.lower()
    if any(char not in "0123456789abcdef" for char in lowered):
        raise ValueError(f"{context}.{key} must be a SHA-256 hex digest")
    return lowered


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
