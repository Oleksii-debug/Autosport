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

from .domain import utc_now_iso


_FORBIDDEN_FUTURE_KEYS = {"final_result", "result", "winner", "settled_outcome", "future_quote"}
_ALLOWED_SPLITS = {"validation", "holdout"}


def _parse_ts(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return parsed


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _FORBIDDEN_FUTURE_KEYS or _contains_forbidden(child) for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(child) for child in value)
    return False


@dataclass(frozen=True, slots=True)
class ForecastRecord:
    quote_key: str
    probability: Decimal
    model_id: str
    model_version: str
    strategy_version: str
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
        if probability < 0 or probability > 1:
            raise ValueError("probability must be between 0 and 1")
        if uncertainty < 0 or uncertainty > 1:
            raise ValueError("uncertainty must be between 0 and 1")
        cutoff = _parse_ts(self.input_cutoff_ts)
        generated = _parse_ts(self.generated_at)
        if cutoff > generated:
            raise ValueError("input cutoff cannot be after forecast generation")
        if _contains_forbidden(self.provenance):
            raise ValueError("forecast provenance must not contain future-result fields")
        if len(set(self.evidence_hashes)) != len(self.evidence_hashes):
            raise ValueError("duplicate evidence hashes")

    @property
    def as_of_ts(self) -> str:
        return self.input_cutoff_ts

    @property
    def canonical_hash(self) -> str:
        canonical = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "quote_key": self.quote_key,
            "probability": str(self.probability),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "strategy_version": self.strategy_version,
            "input_cutoff_ts": self.input_cutoff_ts,
            "generated_at": self.generated_at,
            "uncertainty": str(self.uncertainty),
            "evidence_hashes": list(self.evidence_hashes),
            "market_snapshot_hash": self.market_snapshot_hash,
            "provenance": self.provenance,
        }


class JsonlForecastLedger:
    """Append-only pre-outcome forecast ledger. Settlement/outcome facts never belong in this file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: ForecastRecord) -> str:
        payload = record.to_dict()
        digest = record.canonical_hash
        envelope = json.dumps({"sha256": digest, "record": payload}, ensure_ascii=False, sort_keys=True)
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
        if self.outcome not in (0, 1):
            raise ValueError("outcome must be 0 or 1")
        _parse_ts(self.revealed_at)


@dataclass(frozen=True, slots=True)
class TemporalEvaluationWindow:
    window_id: str
    training_end_ts: str
    evaluation_start_ts: str
    evaluation_end_ts: str
    split: str = "holdout"

    def __post_init__(self) -> None:
        if self.split not in _ALLOWED_SPLITS:
            raise ValueError("split must be validation or holdout")
        training_end = _parse_ts(self.training_end_ts)
        evaluation_start = _parse_ts(self.evaluation_start_ts)
        evaluation_end = _parse_ts(self.evaluation_end_ts)
        if not training_end < evaluation_start <= evaluation_end:
            raise ValueError("window must satisfy training_end < evaluation_start <= evaluation_end")

    def contains(self, generated_at: str) -> bool:
        point = _parse_ts(generated_at)
        return _parse_ts(self.evaluation_start_ts) <= point <= _parse_ts(self.evaluation_end_ts)


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


def evaluate_forecast_window(
    records: Iterable[ForecastRecord],
    outcomes: Iterable[ForecastOutcomeFact],
    window: TemporalEvaluationWindow,
    bins: int = 10,
) -> ForecastEvaluationSummary:
    if bins <= 0:
        raise ValueError("bins must be positive")
    outcome_by_id = {fact.forecast_id: fact for fact in outcomes}
    selected: list[tuple[ForecastRecord, ForecastOutcomeFact]] = []
    for record in records:
        if not window.contains(record.generated_at):
            continue
        fact = outcome_by_id.get(record.forecast_id)
        if fact is None:
            continue
        if _parse_ts(fact.revealed_at) <= _parse_ts(record.generated_at):
            raise ValueError("outcome reveal must be after forecast generation")
        selected.append((record, fact))
    if not selected:
        raise ValueError("no settled forecasts in evaluation window")

    probabilities = [float(record.probability) for record, _fact in selected]
    actuals = [fact.outcome for _record, fact in selected]
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, actuals, strict=True)) / len(selected)
    epsilon = 1e-15
    losses = []
    for p, y in zip(probabilities, actuals, strict=True):
        clipped = min(1.0 - epsilon, max(epsilon, p))
        losses.append(-(y * math.log(clipped) + (1 - y) * math.log(1 - clipped)))
    mean_uncertainty = sum(float(record.uncertainty) for record, _fact in selected) / len(selected)
    calibration = _calibration_bins(probabilities, actuals, bins)
    return ForecastEvaluationSummary(
        window.window_id,
        window.split,
        len(selected),
        brier,
        sum(losses) / len(losses),
        mean_uncertainty,
        calibration,
    )


def _calibration_bins(probabilities: list[float], actuals: list[int], bins: int) -> tuple[CalibrationBin, ...]:
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
                lower,
                upper,
                len(bucket),
                sum(item[0] for item in bucket) / len(bucket),
                sum(item[1] for item in bucket) / len(bucket),
            )
        )
    return tuple(output)
