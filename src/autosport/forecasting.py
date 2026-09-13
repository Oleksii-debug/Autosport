from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


_FORBIDDEN_FUTURE_KEYS = {"final_result", "result", "winner", "settled_outcome", "future_quote"}
_ALLOWED_SPLITS = {"validation", "holdout"}
_SHA256_HEX = frozenset("0123456789abcdef")


def parse_iso_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return parsed


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _FORBIDDEN_FUTURE_KEYS or _contains_forbidden(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(child) for child in value)
    return False


def _canonical_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical SHA-256 digest")
    if len(value) != 64 or any(character not in _SHA256_HEX for character in value):
        raise ValueError(f"{field_name} must be a canonical lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ForecastRecord:
    """Immutable pre-outcome forecast with explicit model/data causal boundaries."""

    quote_key: str
    probability: Decimal
    model_id: str
    model_version: str
    strategy_version: str
    model_training_cutoff_ts: str
    input_cutoff_ts: str
    generated_at: str
    uncertainty: Decimal = Decimal("0")
    evidence_hashes: tuple[str, ...] = field(default_factory=tuple)
    market_snapshot_hash: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    forecast_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        probability = Decimal(str(self.probability))
        uncertainty = Decimal(str(self.uncertainty))
        object.__setattr__(self, "probability", probability)
        object.__setattr__(self, "uncertainty", uncertainty)
        if not self.quote_key or not self.model_id or not self.model_version or not self.strategy_version:
            raise ValueError("forecast identities must not be empty")
        if not probability.is_finite():
            raise ValueError("probability must be finite")
        if not uncertainty.is_finite():
            raise ValueError("uncertainty must be finite")
        if probability < 0 or probability > 1:
            raise ValueError("probability must be between 0 and 1")
        if uncertainty < 0 or uncertainty > 1:
            raise ValueError("uncertainty must be between 0 and 1")
        training_cutoff = parse_iso_timestamp(self.model_training_cutoff_ts)
        input_cutoff = parse_iso_timestamp(self.input_cutoff_ts)
        generated = parse_iso_timestamp(self.generated_at)
        if training_cutoff > input_cutoff:
            raise ValueError("model training cutoff cannot be after forecast input cutoff")
        if input_cutoff > generated:
            raise ValueError("input cutoff cannot be after forecast generation")
        if _contains_forbidden(self.provenance):
            raise ValueError("forecast provenance must not contain future-result fields")
        if not isinstance(self.evidence_hashes, (tuple, list)):
            raise ValueError("evidence_hashes must be an ordered collection of SHA-256 digests")
        evidence_hashes = tuple(
            _canonical_sha256(value, field_name="evidence hash")
            for value in self.evidence_hashes
        )
        if len(set(evidence_hashes)) != len(evidence_hashes):
            raise ValueError("duplicate evidence hashes")
        object.__setattr__(self, "evidence_hashes", evidence_hashes)
        if self.market_snapshot_hash is not None:
            object.__setattr__(
                self,
                "market_snapshot_hash",
                _canonical_sha256(self.market_snapshot_hash, field_name="market_snapshot_hash"),
            )

    @property
    def as_of_ts(self) -> str:
        return self.input_cutoff_ts

    @property
    def canonical_hash(self) -> str:
        canonical = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "quote_key": self.quote_key,
            "probability": str(self.probability),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "strategy_version": self.strategy_version,
            "model_training_cutoff_ts": self.model_training_cutoff_ts,
            "input_cutoff_ts": self.input_cutoff_ts,
            "generated_at": self.generated_at,
            "uncertainty": str(self.uncertainty),
            "evidence_hashes": list(self.evidence_hashes),
            "market_snapshot_hash": self.market_snapshot_hash,
            "provenance": self.provenance,
        }


class JsonlForecastLedger:
    """Append-only pre-outcome forecast ledger. Settlement/outcome facts never belong here."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: ForecastRecord) -> str:
        payload = record.to_dict()
        digest = record.canonical_hash
        envelope = json.dumps(
            {"sha256": digest, "record": payload}, ensure_ascii=False, sort_keys=True
        )
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(envelope + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return digest


@dataclass(frozen=True, slots=True)
class ForecastOutcomeFact:
    forecast_id: str
    outcome: int
    revealed_at: str

    def __post_init__(self) -> None:
        if not self.forecast_id:
            raise ValueError("forecast_id required")
        if self.outcome not in (0, 1):
            raise ValueError("outcome must be 0 or 1")
        parse_iso_timestamp(self.revealed_at)


@dataclass(frozen=True, slots=True)
class TemporalEvaluationWindow:
    """One validation/holdout fold. Multiple ordered windows form walk-forward evaluation."""

    window_id: str
    training_end_ts: str
    evaluation_start_ts: str
    evaluation_end_ts: str
    split: str = "holdout"

    def __post_init__(self) -> None:
        if not self.window_id:
            raise ValueError("window_id required")
        if self.split not in _ALLOWED_SPLITS:
            raise ValueError("split must be validation or holdout")
        training_end = parse_iso_timestamp(self.training_end_ts)
        evaluation_start = parse_iso_timestamp(self.evaluation_start_ts)
        evaluation_end = parse_iso_timestamp(self.evaluation_end_ts)
        if not training_end < evaluation_start <= evaluation_end:
            raise ValueError("window must satisfy training_end < evaluation_start <= evaluation_end")

    def contains(self, generated_at: str) -> bool:
        point = parse_iso_timestamp(generated_at)
        return (
            parse_iso_timestamp(self.evaluation_start_ts)
            <= point
            <= parse_iso_timestamp(self.evaluation_end_ts)
        )


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_probability: float
    observed_rate: float


@dataclass(frozen=True, slots=True)
class ForecastEvaluationSummary:
    window_id: str
    split: str
    count: int
    brier_score: float
    log_loss: float
    mean_uncertainty: float
    calibration: tuple[CalibrationBin, ...]
    model_versions: tuple[str, ...]
    strategy_versions: tuple[str, ...]


def evaluate_forecast_window(
    records: Iterable[ForecastRecord],
    outcomes: Iterable[ForecastOutcomeFact],
    window: TemporalEvaluationWindow,
    bins: int = 10,
) -> ForecastEvaluationSummary:
    """Evaluate one temporal fold without allowing model-training leakage across its boundary."""

    if bins <= 0:
        raise ValueError("bins must be positive")
    outcome_by_id: dict[str, ForecastOutcomeFact] = {}
    for fact in outcomes:
        if fact.forecast_id in outcome_by_id:
            raise ValueError(f"duplicate outcome fact for forecast: {fact.forecast_id}")
        outcome_by_id[fact.forecast_id] = fact

    training_end = parse_iso_timestamp(window.training_end_ts)
    selected: list[tuple[ForecastRecord, ForecastOutcomeFact]] = []
    selected_ids: set[str] = set()
    for record in records:
        if record.forecast_id in selected_ids:
            raise ValueError(f"duplicate forecast_id: {record.forecast_id}")
        if not window.contains(record.generated_at):
            continue
        selected_ids.add(record.forecast_id)
        if parse_iso_timestamp(record.model_training_cutoff_ts) > training_end:
            raise ValueError(
                "model training cutoff crosses evaluation boundary; holdout/walk-forward leakage"
            )
        fact = outcome_by_id.get(record.forecast_id)
        if fact is None:
            continue
        if parse_iso_timestamp(fact.revealed_at) <= parse_iso_timestamp(record.generated_at):
            raise ValueError("outcome reveal must be after forecast generation")
        selected.append((record, fact))
    if not selected:
        raise ValueError("no settled forecasts in evaluation window")

    probabilities = [float(record.probability) for record, _fact in selected]
    actuals = [fact.outcome for _record, fact in selected]
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, actuals, strict=True)) / len(selected)
    epsilon = 1e-15
    losses = []
    for probability, outcome in zip(probabilities, actuals, strict=True):
        clipped = min(1.0 - epsilon, max(epsilon, probability))
        losses.append(
            -(outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped))
        )
    mean_uncertainty = sum(
        float(record.uncertainty) for record, _fact in selected
    ) / len(selected)
    calibration = _calibration_bins(probabilities, actuals, bins)
    return ForecastEvaluationSummary(
        window_id=window.window_id,
        split=window.split,
        count=len(selected),
        brier_score=brier,
        log_loss=sum(losses) / len(losses),
        mean_uncertainty=mean_uncertainty,
        calibration=calibration,
        model_versions=tuple(sorted({record.model_version for record, _ in selected})),
        strategy_versions=tuple(
            sorted({record.strategy_version for record, _ in selected})
        ),
    )


def evaluate_walk_forward(
    records: Iterable[ForecastRecord],
    outcomes: Iterable[ForecastOutcomeFact],
    windows: Iterable[TemporalEvaluationWindow],
    bins: int = 10,
) -> tuple[ForecastEvaluationSummary, ...]:
    """Evaluate ordered non-overlapping temporal folds using the same leakage rules per fold."""

    record_values = tuple(records)
    outcome_values = tuple(outcomes)
    window_values = tuple(windows)
    if not window_values:
        raise ValueError("walk-forward windows required")
    ordered = sorted(window_values, key=lambda item: parse_iso_timestamp(item.evaluation_start_ts))
    previous_end: datetime | None = None
    for window in ordered:
        start = parse_iso_timestamp(window.evaluation_start_ts)
        end = parse_iso_timestamp(window.evaluation_end_ts)
        if previous_end is not None and start <= previous_end:
            raise ValueError("walk-forward evaluation windows must not overlap")
        previous_end = end
    return tuple(
        evaluate_forecast_window(record_values, outcome_values, window, bins=bins)
        for window in ordered
    )


def _calibration_bins(
    probabilities: list[float], actuals: list[int], bins: int
) -> tuple[CalibrationBin, ...]:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for probability, outcome in zip(probabilities, actuals, strict=True):
        index = min(bins - 1, int(probability * bins))
        buckets[index].append((probability, outcome))
    output: list[CalibrationBin] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        lower = index / bins
        upper = (index + 1) / bins
        output.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(bucket),
                mean_probability=sum(item[0] for item in bucket) / len(bucket),
                observed_rate=sum(item[1] for item in bucket) / len(bucket),
            )
        )
    return tuple(output)
