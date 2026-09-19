"""Durable causal drift evidence for Autosport scientific learning.

This module is diagnostic control, not production-promotion or financial authority.
It reuses ScientificRegistry for immutable persistence and exposes a single
ResearchSupervisor binding seam. Arithmetic is exact over canonical decimal
samples; interpretation remains explicitly statistical evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction
from typing import Any, Mapping

from .scientific_registry import RegistryEntry, ScientificRegistry


DRIFT_SCHEMA_VERSION = 1
DRIFT_ALGORITHM_VERSION = "autosport.drift.mean-absolute-shift.v1"
_HEX = frozenset("0123456789abcdef")


class DriftControlError(RuntimeError):
    """Base error for drift evidence/control failures."""


class DriftCausalityError(DriftControlError):
    """Evidence was unavailable at the declared causal boundary."""


class DriftLineageError(DriftControlError):
    """Drift evidence does not match the declared scientific lineage."""


class DriftKind(StrEnum):
    COVARIATE = "COVARIATE"
    FORECAST_PERFORMANCE = "FORECAST_PERFORMANCE"
    EXECUTION_MARKET_STATE = "EXECUTION_MARKET_STATE"


class DriftMetric(StrEnum):
    MEAN_ABSOLUTE_SHIFT = "MEAN_ABSOLUTE_SHIFT"


class DriftState(StrEnum):
    DRIFT_DETECTED = "DRIFT_DETECTED"
    NO_DRIFT = "NO_DRIFT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class DriftRecommendation(StrEnum):
    NONE = "NONE"
    RESEARCH_RETRAIN_CHALLENGER = "RESEARCH_RETRAIN_CHALLENGER"
    POSTMORTEM_REVIEW = "POSTMORTEM_REVIEW"


class DriftTruth(StrEnum):
    STATISTICAL_EVIDENCE_ONLY = "STATISTICAL_EVIDENCE_ONLY"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{name} must be canonical SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_identity(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_scope(
    *,
    sport: object | None,
    league: object | None,
    regime: object | None,
) -> tuple[str, str, str] | None:
    values = (sport, league, regime)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("sport, league and regime must be provided together")
    return (
        _text(sport, "sport"),
        _text(league, "league"),
        _text(regime, "regime"),
    )


def _scope_from_payload(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    try:
        return _canonical_scope(
            sport=payload.get("sport"),
            league=payload.get("league"),
            regime=payload.get("regime"),
        )
    except ValueError as exc:
        raise DriftLineageError("drift scope payload is invalid") from exc


def _canonical_decimal(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite canonical decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    if parsed == 0:
        canonical = "0"
    else:
        # Decimal.normalize() uses the ambient process context and can round.
        canonical = format(parsed, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        if canonical == "-0":
            canonical = "0"
    if text != canonical:
        raise ValueError(f"{name} must be canonical decimal text: {canonical}")
    return canonical


def _canonical_effective_sample_size(
    value: object | None,
    sample_count: int,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("effective_sample_size must be a positive integer")
    if value > sample_count:
        raise ValueError("effective_sample_size cannot exceed sample_count")
    return value


def _window_evidence_sha256(
    *,
    dataset_snapshot_id: object,
    source_identity: object,
    window_start: object,
    window_end: object,
    as_of: object,
    values: tuple[str, ...],
    value_observed_at: tuple[str, ...],
    value_available_at: tuple[str, ...],
    effective_sample_size: object | None = None,
    sport: object | None = None,
    league: object | None = None,
    regime: object | None = None,
) -> str:
    payload: dict[str, Any] = {
        "schema": "autosport.drift-window-evidence",
        "schema_version": 1,
        "dataset_snapshot_id": _text(dataset_snapshot_id, "dataset_snapshot_id"),
        "source_identity": _text(source_identity, "source_identity"),
        "window_start": _timestamp_identity(window_start, "window_start"),
        "window_end": _timestamp_identity(window_end, "window_end"),
        "as_of": _timestamp_identity(as_of, "as_of"),
        "values": [
            _canonical_decimal(value, f"values[{index}]")
            for index, value in enumerate(values)
        ],
        "value_observed_at": [
            _timestamp_identity(value, f"value_observed_at[{index}]")
            for index, value in enumerate(value_observed_at)
        ],
        "value_available_at": [
            _timestamp_identity(value, f"value_available_at[{index}]")
            for index, value in enumerate(value_available_at)
        ],
    }
    effective = _canonical_effective_sample_size(
        effective_sample_size,
        len(values),
    )
    if effective is not None:
        payload["effective_sample_size"] = effective
    scope = _canonical_scope(sport=sport, league=league, regime=regime)
    if scope is not None:
        payload["sport"], payload["league"], payload["regime"] = scope
    return _digest(payload)


def _fraction_from_decimal(value: str, name: str) -> Fraction:
    return Fraction(Decimal(_canonical_decimal(value, name)))


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _fraction_from_text(value: object, name: str) -> Fraction:
    text = _text(value, name)
    pieces = text.split("/")
    if len(pieces) != 2:
        raise ValueError(f"{name} must be canonical numerator/denominator")
    try:
        numerator = int(pieces[0])
        denominator = int(pieces[1])
    except ValueError as exc:
        raise ValueError(f"{name} must be canonical numerator/denominator") from exc
    if denominator <= 0:
        raise ValueError(f"{name} denominator must be positive")
    parsed = Fraction(numerator, denominator)
    if text != _fraction_text(parsed):
        raise ValueError(f"{name} must be reduced canonical fraction")
    return parsed


def _mean_fraction(values: tuple[str, ...]) -> Fraction | None:
    if not values:
        return None
    total = sum((_fraction_from_decimal(value, "drift value") for value in values), Fraction())
    return total / len(values)


@dataclass(frozen=True, slots=True)
class DriftWindow:
    dataset_snapshot_id: str
    source_identity: str
    revision_id: str
    window_start: str
    window_end: str
    as_of: str
    values: tuple[str, ...]
    value_observed_at: tuple[str, ...]
    value_available_at: tuple[str, ...]
    evidence_sha256: str
    effective_sample_size: int | None = None
    sport: str | None = None
    league: str | None = None
    regime: str | None = None

    def __post_init__(self) -> None:
        for name in ("dataset_snapshot_id", "source_identity"):
            _text(getattr(self, name), name)
        revision = _sha256(self.revision_id, "revision_id")
        _canonical_scope(sport=self.sport, league=self.league, regime=self.regime)
        start = _instant(self.window_start, "window_start")
        end = _instant(self.window_end, "window_end")
        cutoff = _instant(self.as_of, "as_of")
        if end < start:
            raise ValueError("window_end must not precede window_start")
        if cutoff < end:
            raise DriftCausalityError("window end is later than its causal as_of boundary")
        if (
            type(self.values) is not tuple
            or type(self.value_observed_at) is not tuple
            or type(self.value_available_at) is not tuple
        ):
            raise ValueError(
                "values, value_observed_at and value_available_at must be tuples"
            )
        if not (
            len(self.values)
            == len(self.value_observed_at)
            == len(self.value_available_at)
        ):
            raise ValueError(
                "each drift value requires one observation and availability timestamp"
            )
        for index, value in enumerate(self.values):
            _canonical_decimal(value, f"values[{index}]")
        _canonical_effective_sample_size(
            self.effective_sample_size,
            len(self.values),
        )
        for index, observed_at in enumerate(self.value_observed_at):
            observed = _instant(observed_at, f"value_observed_at[{index}]")
            if observed < start or observed > end:
                raise DriftCausalityError(
                    "drift sample observation is outside the declared window"
                )
            available = _instant(
                self.value_available_at[index], f"value_available_at[{index}]"
            )
            if available < observed:
                raise DriftCausalityError(
                    "drift value cannot be available before it was observed"
                )
            if available > cutoff:
                raise DriftCausalityError(
                    "drift window contains a value unavailable at its causal as_of boundary"
                )
        expected_evidence = _window_evidence_sha256(
            dataset_snapshot_id=self.dataset_snapshot_id,
            source_identity=self.source_identity,
            window_start=self.window_start,
            window_end=self.window_end,
            as_of=self.as_of,
            values=self.values,
            value_observed_at=self.value_observed_at,
            value_available_at=self.value_available_at,
            effective_sample_size=self.effective_sample_size,
            sport=self.sport,
            league=self.league,
            regime=self.regime,
        )
        evidence = _sha256(self.evidence_sha256, "evidence_sha256")
        if evidence != expected_evidence:
            raise ValueError(
                "evidence_sha256 does not match exact drift window sample membership"
            )
        if revision != evidence:
            raise ValueError(
                "revision_id must equal the canonical immutable drift evidence identity"
            )

    @classmethod
    def from_samples(
        cls,
        *,
        dataset_snapshot_id: str,
        source_identity: str,
        window_start: str,
        window_end: str,
        as_of: str,
        values: tuple[str, ...],
        value_observed_at: tuple[str, ...],
        value_available_at: tuple[str, ...],
        effective_sample_size: int | None = None,
        sport: str | None = None,
        league: str | None = None,
        regime: str | None = None,
    ) -> "DriftWindow":
        evidence = _window_evidence_sha256(
            dataset_snapshot_id=dataset_snapshot_id,
            source_identity=source_identity,
            window_start=window_start,
            window_end=window_end,
            as_of=as_of,
            values=values,
            value_observed_at=value_observed_at,
            value_available_at=value_available_at,
            effective_sample_size=effective_sample_size,
            sport=sport,
            league=league,
            regime=regime,
        )
        return cls(
            dataset_snapshot_id=dataset_snapshot_id,
            source_identity=source_identity,
            revision_id=evidence,
            window_start=window_start,
            window_end=window_end,
            as_of=as_of,
            values=values,
            value_observed_at=value_observed_at,
            value_available_at=value_available_at,
            evidence_sha256=evidence,
            effective_sample_size=effective_sample_size,
            sport=sport,
            league=league,
            regime=regime,
        )

    @property
    def sample_count(self) -> int:
        return len(self.values)

    @property
    def mean_fraction(self) -> str | None:
        value = _mean_fraction(self.values)
        return None if value is None else _fraction_text(value)


@dataclass(frozen=True, slots=True)
class DriftReference:
    drift_kind: DriftKind
    metric: DriftMetric
    model_version_id: str
    strategy_version_id: str
    feature_set_id: str
    experiment_id: str
    baseline_dataset_snapshot_id: str
    source_identity: str
    revision_id: str
    window_start: str
    window_end: str
    baseline_as_of: str
    training_cutoff: str
    min_samples: int
    threshold: str
    metric_definition_sha256: str
    evidence_sha256: str
    sample_count: int
    mean_fraction: str | None
    sport: str | None = None
    league: str | None = None
    regime: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.drift_kind, DriftKind):
            raise ValueError("drift_kind must be DriftKind")
        if not isinstance(self.metric, DriftMetric):
            raise ValueError("metric must be DriftMetric")
        for name in (
            "model_version_id",
            "strategy_version_id",
            "feature_set_id",
            "experiment_id",
            "baseline_dataset_snapshot_id",
            "source_identity",
            "revision_id",
        ):
            _text(getattr(self, name), name)
        start = _instant(self.window_start, "window_start")
        end = _instant(self.window_end, "window_end")
        as_of = _instant(self.baseline_as_of, "baseline_as_of")
        training = _instant(self.training_cutoff, "training_cutoff")
        if end < start or as_of < end:
            raise ValueError("baseline window timestamps are inconsistent")
        if training > start:
            raise DriftCausalityError("baseline window predates the model training cutoff")
        if isinstance(self.min_samples, bool) or not isinstance(self.min_samples, int):
            raise ValueError("min_samples must be an integer")
        if self.min_samples <= 0:
            raise ValueError("min_samples must be positive")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int):
            raise ValueError("sample_count must be an integer")
        if self.sample_count < 0:
            raise ValueError("sample_count must be non-negative")
        threshold = _fraction_from_decimal(self.threshold, "threshold")
        if threshold < 0:
            raise ValueError("threshold must be non-negative")
        _sha256(self.metric_definition_sha256, "metric_definition_sha256")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _canonical_scope(sport=self.sport, league=self.league, regime=self.regime)
        if self.sample_count == 0:
            if self.mean_fraction is not None:
                raise ValueError("empty baseline cannot carry a mean")
        elif self.mean_fraction is None:
            raise ValueError("non-empty baseline requires a mean")
        else:
            _fraction_from_text(self.mean_fraction, "mean_fraction")

    @property
    def record_type(self) -> str:
        return "DriftReference"

    @property
    def available_at(self) -> str:
        return _timestamp_identity(self.baseline_as_of, "baseline_as_of")

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": DRIFT_SCHEMA_VERSION,
            "drift_kind": self.drift_kind.value,
            "metric": self.metric.value,
            "model_version_id": self.model_version_id,
            "strategy_version_id": self.strategy_version_id,
            "feature_set_id": self.feature_set_id,
            "experiment_id": self.experiment_id,
            "baseline_dataset_snapshot_id": self.baseline_dataset_snapshot_id,
            "source_identity": self.source_identity,
            "revision_id": self.revision_id,
            "window_start": _timestamp_identity(self.window_start, "window_start"),
            "window_end": _timestamp_identity(self.window_end, "window_end"),
            "baseline_as_of": _timestamp_identity(self.baseline_as_of, "baseline_as_of"),
            "training_cutoff": _timestamp_identity(self.training_cutoff, "training_cutoff"),
            "min_samples": self.min_samples,
            "threshold": self.threshold,
            "metric_definition_sha256": self.metric_definition_sha256.lower(),
            "evidence_sha256": self.evidence_sha256.lower(),
            "sample_count": self.sample_count,
            "mean_fraction": self.mean_fraction,
            "truth": DriftTruth.STATISTICAL_EVIDENCE_ONLY.value,
            "arithmetic_truth": "EXACT_RATIONAL_FROM_CANONICAL_DECIMALS",
            "interpretation_assumption": "THRESHOLD_DIAGNOSTIC_NOT_SIGNIFICANCE_TEST",
        }
        scope = _canonical_scope(sport=self.sport, league=self.league, regime=self.regime)
        if scope is not None:
            payload["sport"], payload["league"], payload["regime"] = scope
        return payload

    @property
    def reference_id(self) -> str:
        return _digest(self.to_payload())

    @property
    def record_id(self) -> str:
        return self.reference_id


@dataclass(frozen=True, slots=True)
class DriftObservation:
    reference_id: str
    dataset_snapshot_id: str
    source_identity: str
    revision_id: str
    window_start: str
    window_end: str
    observation_as_of: str
    evidence_sha256: str
    sample_count: int
    mean_fraction: str | None
    effective_sample_size: int | None = None
    sport: str | None = None
    league: str | None = None
    regime: str | None = None

    def __post_init__(self) -> None:
        for name in ("reference_id", "dataset_snapshot_id", "source_identity", "revision_id"):
            _text(getattr(self, name), name)
        start = _instant(self.window_start, "window_start")
        end = _instant(self.window_end, "window_end")
        as_of = _instant(self.observation_as_of, "observation_as_of")
        if end < start or as_of < end:
            raise ValueError("observation window timestamps are inconsistent")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _canonical_scope(sport=self.sport, league=self.league, regime=self.regime)
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int):
            raise ValueError("sample_count must be an integer")
        if self.sample_count < 0:
            raise ValueError("sample_count must be non-negative")
        _canonical_effective_sample_size(
            self.effective_sample_size,
            self.sample_count,
        )
        if self.sample_count == 0:
            if self.mean_fraction is not None:
                raise ValueError("empty observation cannot carry a mean")
        elif self.mean_fraction is None:
            raise ValueError("non-empty observation requires a mean")
        else:
            _fraction_from_text(self.mean_fraction, "mean_fraction")

    @property
    def record_type(self) -> str:
        return "DriftObservation"

    @property
    def available_at(self) -> str:
        return _timestamp_identity(self.observation_as_of, "observation_as_of")

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": DRIFT_SCHEMA_VERSION,
            "reference_id": self.reference_id,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "source_identity": self.source_identity,
            "revision_id": self.revision_id,
            "window_start": _timestamp_identity(self.window_start, "window_start"),
            "window_end": _timestamp_identity(self.window_end, "window_end"),
            "observation_as_of": _timestamp_identity(
                self.observation_as_of, "observation_as_of"
            ),
            "evidence_sha256": self.evidence_sha256.lower(),
            "sample_count": self.sample_count,
            "mean_fraction": self.mean_fraction,
        }
        if self.effective_sample_size is not None:
            payload["effective_sample_size"] = self.effective_sample_size
        scope = _canonical_scope(sport=self.sport, league=self.league, regime=self.regime)
        if scope is not None:
            payload["sport"], payload["league"], payload["regime"] = scope
        return payload

    @property
    def observation_id(self) -> str:
        return _digest(self.to_payload())

    @property
    def record_id(self) -> str:
        return self.observation_id


@dataclass(frozen=True, slots=True)
class DriftFinding:
    finding_id: str
    reference_id: str
    observation_id: str
    drift_kind: DriftKind
    metric: DriftMetric
    model_version_id: str
    strategy_version_id: str
    feature_set_id: str
    experiment_id: str
    state: DriftState
    recommendation: DriftRecommendation
    absolute_delta_fraction: str | None
    threshold: str
    insufficiency_reason: str | None
    evaluated_at: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "finding_id",
            "reference_id",
            "observation_id",
            "model_version_id",
            "strategy_version_id",
            "feature_set_id",
            "experiment_id",
        ):
            _text(getattr(self, name), name)
        if not isinstance(self.drift_kind, DriftKind):
            raise ValueError("drift_kind must be DriftKind")
        if not isinstance(self.metric, DriftMetric):
            raise ValueError("metric must be DriftMetric")
        if not isinstance(self.state, DriftState):
            raise ValueError("state must be DriftState")
        if not isinstance(self.recommendation, DriftRecommendation):
            raise ValueError("recommendation must be DriftRecommendation")
        threshold = _fraction_from_decimal(self.threshold, "threshold")
        if threshold < 0:
            raise ValueError("threshold must be non-negative")
        if self.absolute_delta_fraction is not None:
            delta = _fraction_from_text(
                self.absolute_delta_fraction, "absolute_delta_fraction"
            )
            if delta < 0:
                raise ValueError("absolute_delta_fraction must be non-negative")
        if self.state is DriftState.INSUFFICIENT_EVIDENCE:
            if self.insufficiency_reason is None:
                raise ValueError("insufficient evidence requires a reason")
            _text(self.insufficiency_reason, "insufficiency_reason")
            if self.absolute_delta_fraction is not None:
                raise ValueError("insufficient evidence cannot claim a drift magnitude")
        else:
            if self.insufficiency_reason is not None:
                raise ValueError("sufficient evidence cannot carry insufficiency_reason")
            if self.absolute_delta_fraction is None:
                raise ValueError("sufficient evidence requires a drift magnitude")
        if self.state is not DriftState.DRIFT_DETECTED:
            if self.recommendation is not DriftRecommendation.NONE:
                raise ValueError("only detected drift may recommend follow-up work")
        elif self.recommendation is DriftRecommendation.NONE:
            raise ValueError("detected drift requires a bounded recommendation")
        _instant(self.evaluated_at, "evaluated_at")
        _sha256(self.evidence_sha256, "evidence_sha256")
        expected_id = _digest(
            {
                "schema_version": DRIFT_SCHEMA_VERSION,
                "algorithm_version": DRIFT_ALGORITHM_VERSION,
                "reference_id": self.reference_id,
                "observation_id": self.observation_id,
            }
        )
        if self.finding_id != expected_id:
            raise ValueError("finding_id does not match deterministic drift identity")

    @property
    def record_type(self) -> str:
        return "DriftFinding"

    @property
    def record_id(self) -> str:
        return self.finding_id

    @property
    def available_at(self) -> str:
        return _timestamp_identity(self.evaluated_at, "evaluated_at")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": DRIFT_SCHEMA_VERSION,
            "algorithm_version": DRIFT_ALGORITHM_VERSION,
            "finding_id": self.finding_id,
            "reference_id": self.reference_id,
            "observation_id": self.observation_id,
            "drift_kind": self.drift_kind.value,
            "metric": self.metric.value,
            "model_version_id": self.model_version_id,
            "strategy_version_id": self.strategy_version_id,
            "feature_set_id": self.feature_set_id,
            "experiment_id": self.experiment_id,
            "state": self.state.value,
            "recommendation": self.recommendation.value,
            "absolute_delta_fraction": self.absolute_delta_fraction,
            "threshold": self.threshold,
            "insufficiency_reason": self.insufficiency_reason,
            "evaluated_at": _timestamp_identity(self.evaluated_at, "evaluated_at"),
            "evidence_sha256": self.evidence_sha256.lower(),
            "truth": DriftTruth.STATISTICAL_EVIDENCE_ONLY.value,
            "automatic_promotion_authorized": False,
            "financial_authority_change_authorized": False,
            "real_money_execution_authorized": False,
        }


class DriftMonitor:
    """Create immutable drift evidence and bounded scientific recommendations."""

    def __init__(self, scientific_registry: ScientificRegistry) -> None:
        if not isinstance(scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        self.scientific_registry = scientific_registry

    def _require_record(
        self,
        record_type: str,
        record_id: str,
        *,
        as_of: str,
    ) -> RegistryEntry:
        entry = self.scientific_registry.get(record_type, record_id)
        if entry is None:
            raise DriftLineageError(f"missing {record_type}:{record_id}")
        cutoff = _instant(as_of, "as_of")
        if _instant(entry.available_at, f"{record_type}.available_at") > cutoff:
            raise DriftCausalityError(
                f"{record_type}:{record_id} was unavailable at the causal boundary"
            )
        reveal_after = entry.reveal_after
        if reveal_after is not None and _instant(
            reveal_after, f"{record_type}.reveal_after"
        ) > cutoff:
            raise DriftCausalityError(
                f"{record_type}:{record_id} was unrevealed at the causal boundary"
            )
        return entry

    def _validate_window_dataset(self, window: DriftWindow) -> RegistryEntry:
        entry = self._require_record(
            "DatasetSnapshot", window.dataset_snapshot_id, as_of=window.as_of
        )
        payload = entry.payload
        if payload.get("source_identity") != window.source_identity:
            raise DriftLineageError(
                "drift window source identity does not match DatasetSnapshot"
            )
        causal_cutoff = payload.get("causal_cutoff")
        if not isinstance(causal_cutoff, str):
            raise DriftLineageError("DatasetSnapshot lacks causal cutoff")
        cutoff = _instant(causal_cutoff, "DatasetSnapshot.causal_cutoff")
        if cutoff < _instant(window.window_end, "window.window_end"):
            raise DriftCausalityError(
                "DatasetSnapshot does not cover drift window_end"
            )
        if cutoff > _instant(window.as_of, "window.as_of"):
            raise DriftCausalityError(
                "DatasetSnapshot causal cutoff is later than drift window as_of"
            )
        manifest = payload.get("manifest_sha256")
        if not isinstance(manifest, str):
            raise DriftLineageError("DatasetSnapshot lacks manifest identity")
        if _sha256(manifest, "DatasetSnapshot.manifest_sha256") != window.evidence_sha256:
            raise DriftLineageError(
                "drift window evidence does not match DatasetSnapshot manifest"
            )
        if window.revision_id != window.evidence_sha256:
            raise DriftLineageError(
                "drift revision identity is not bound to canonical window evidence"
            )
        return entry

    def create_reference(
        self,
        *,
        drift_kind: DriftKind,
        metric: DriftMetric,
        model_version_id: str,
        strategy_version_id: str,
        feature_set_id: str,
        experiment_id: str,
        baseline: DriftWindow,
        min_samples: int,
        threshold: str,
        metric_definition_sha256: str,
    ) -> DriftReference:
        if not isinstance(drift_kind, DriftKind):
            raise TypeError("drift_kind must be DriftKind")
        if not isinstance(metric, DriftMetric):
            raise TypeError("metric must be DriftMetric")
        if metric is not DriftMetric.MEAN_ABSOLUTE_SHIFT:
            raise ValueError("unsupported drift metric")
        if isinstance(min_samples, bool) or not isinstance(min_samples, int):
            raise ValueError("min_samples must be an integer")
        if min_samples <= 0:
            raise ValueError("min_samples must be positive")
        canonical_threshold = _canonical_decimal(threshold, "threshold")
        if _fraction_from_decimal(canonical_threshold, "threshold") < 0:
            raise ValueError("threshold must be non-negative")
        metric_definition = _sha256(
            metric_definition_sha256, "metric_definition_sha256"
        )

        model = self._require_record(
            "ModelVersion", model_version_id, as_of=baseline.as_of
        )
        strategy = self._require_record(
            "StrategyVersion", strategy_version_id, as_of=baseline.as_of
        )
        feature = self._require_record(
            "FeatureSet", feature_set_id, as_of=baseline.as_of
        )
        experiment = self._require_record(
            "Experiment", experiment_id, as_of=baseline.as_of
        )
        self._validate_window_dataset(baseline)

        if model.payload.get("feature_set_id") != feature.record_id:
            raise DriftLineageError("model/feature identity mismatch")
        if strategy.payload.get("model_version_id") != model.record_id:
            raise DriftLineageError("strategy/model identity mismatch")
        if experiment.payload.get("model_version_id") != model.record_id:
            raise DriftLineageError("experiment/model identity mismatch")
        if experiment.payload.get("strategy_version_id") != strategy.record_id:
            raise DriftLineageError("experiment/strategy identity mismatch")
        if experiment.payload.get("feature_set_id") != feature.record_id:
            raise DriftLineageError("experiment/feature identity mismatch")
        if experiment.payload.get("dataset_snapshot_id") != baseline.dataset_snapshot_id:
            raise DriftLineageError("experiment/baseline dataset identity mismatch")
        if model.payload.get("research_protocol_id") != experiment.payload.get(
            "research_protocol_id"
        ):
            raise DriftLineageError("model/experiment research protocol mismatch")

        training_dataset_id = model.payload.get("dataset_snapshot_id")
        if not isinstance(training_dataset_id, str):
            raise DriftLineageError("model lacks training dataset identity")
        training_dataset = self._require_record(
            "DatasetSnapshot", training_dataset_id, as_of=baseline.as_of
        )
        training_cutoff = training_dataset.payload.get("causal_cutoff")
        if not isinstance(training_cutoff, str):
            raise DriftLineageError("training DatasetSnapshot lacks causal cutoff")
        if _instant(training_cutoff, "training_cutoff") > _instant(
            baseline.window_start, "baseline.window_start"
        ):
            raise DriftCausalityError(
                "baseline window predates the model training cutoff"
            )

        reference = DriftReference(
            drift_kind=drift_kind,
            metric=metric,
            model_version_id=model.record_id,
            strategy_version_id=strategy.record_id,
            feature_set_id=feature.record_id,
            experiment_id=experiment.record_id,
            baseline_dataset_snapshot_id=baseline.dataset_snapshot_id,
            source_identity=baseline.source_identity,
            revision_id=baseline.revision_id,
            window_start=baseline.window_start,
            window_end=baseline.window_end,
            baseline_as_of=baseline.as_of,
            training_cutoff=training_cutoff,
            min_samples=min_samples,
            threshold=canonical_threshold,
            metric_definition_sha256=metric_definition,
            evidence_sha256=baseline.evidence_sha256,
            sample_count=baseline.sample_count,
            mean_fraction=baseline.mean_fraction,
            sport=baseline.sport,
            league=baseline.league,
            regime=baseline.regime,
        )
        self.scientific_registry.append(reference)
        return reference

    def evaluate(
        self,
        reference_id: str,
        current: DriftWindow,
        *,
        evaluated_at: str,
    ) -> DriftFinding:
        reference_entry = self._require_record(
            "DriftReference", reference_id, as_of=evaluated_at
        )
        reference = reference_entry.payload
        if reference.get("schema_version") != DRIFT_SCHEMA_VERSION:
            raise DriftLineageError("unsupported drift reference schema")
        if _instant(current.as_of, "current.as_of") > _instant(
            evaluated_at, "evaluated_at"
        ):
            raise DriftCausalityError(
                "current drift window is later than finding evaluation time"
            )
        if _instant(reference["baseline_as_of"], "baseline_as_of") > _instant(
            current.as_of, "current.as_of"
        ):
            raise DriftCausalityError(
                "current drift window cannot precede its baseline reference"
            )
        self._validate_window_dataset(current)

        observation = DriftObservation(
            reference_id=reference_id,
            dataset_snapshot_id=current.dataset_snapshot_id,
            source_identity=current.source_identity,
            revision_id=current.revision_id,
            window_start=current.window_start,
            window_end=current.window_end,
            observation_as_of=current.as_of,
            evidence_sha256=current.evidence_sha256,
            sample_count=current.sample_count,
            mean_fraction=current.mean_fraction,
            effective_sample_size=current.effective_sample_size,
            sport=current.sport,
            league=current.league,
            regime=current.regime,
        )
        observation_sha = self.scientific_registry.append(observation)

        min_samples = reference.get("min_samples")
        if isinstance(min_samples, bool) or not isinstance(min_samples, int):
            raise DriftLineageError("drift reference has invalid min_samples")
        baseline_count = reference.get("sample_count")
        if isinstance(baseline_count, bool) or not isinstance(baseline_count, int):
            raise DriftLineageError("drift reference has invalid sample_count")

        insufficiency_reason: str | None = None
        if baseline_count < min_samples:
            insufficiency_reason = "REFERENCE_SAMPLE_COUNT"
        elif current.sample_count < min_samples:
            insufficiency_reason = "CURRENT_SAMPLE_COUNT"
        elif current.source_identity != reference.get("source_identity"):
            insufficiency_reason = "SOURCE_IDENTITY_MISMATCH"
        elif _canonical_scope(
            sport=current.sport,
            league=current.league,
            regime=current.regime,
        ) != _scope_from_payload(reference):
            insufficiency_reason = "SCOPE_MISMATCH"

        delta_text: str | None
        if insufficiency_reason is not None:
            state = DriftState.INSUFFICIENT_EVIDENCE
            recommendation = DriftRecommendation.NONE
            delta_text = None
        else:
            baseline_mean_raw = reference.get("mean_fraction")
            if not isinstance(baseline_mean_raw, str) or current.mean_fraction is None:
                raise DriftLineageError("sufficient drift windows must carry means")
            baseline_mean = _fraction_from_text(
                baseline_mean_raw, "reference.mean_fraction"
            )
            current_mean = _fraction_from_text(
                current.mean_fraction, "current.mean_fraction"
            )
            delta = abs(current_mean - baseline_mean)
            delta_text = _fraction_text(delta)
            threshold = _fraction_from_decimal(reference["threshold"], "threshold")
            if delta > threshold:
                state = DriftState.DRIFT_DETECTED
                if reference.get("drift_kind") == DriftKind.EXECUTION_MARKET_STATE.value:
                    recommendation = DriftRecommendation.POSTMORTEM_REVIEW
                else:
                    recommendation = DriftRecommendation.RESEARCH_RETRAIN_CHALLENGER
            else:
                state = DriftState.NO_DRIFT
                recommendation = DriftRecommendation.NONE

        finding_id = _digest(
            {
                "schema_version": DRIFT_SCHEMA_VERSION,
                "algorithm_version": DRIFT_ALGORITHM_VERSION,
                "reference_id": reference_id,
                "observation_id": observation.observation_id,
            }
        )
        evidence_sha256 = _digest(
            {
                "algorithm_version": DRIFT_ALGORITHM_VERSION,
                "reference_record_sha256": reference_entry.record_sha256,
                "observation_record_sha256": observation_sha,
                "state": state.value,
                "recommendation": recommendation.value,
                "absolute_delta_fraction": delta_text,
                "insufficiency_reason": insufficiency_reason,
            }
        )
        finding = DriftFinding(
            finding_id=finding_id,
            reference_id=reference_id,
            observation_id=observation.observation_id,
            drift_kind=DriftKind(reference["drift_kind"]),
            metric=DriftMetric(reference["metric"]),
            model_version_id=reference["model_version_id"],
            strategy_version_id=reference["strategy_version_id"],
            feature_set_id=reference["feature_set_id"],
            experiment_id=reference["experiment_id"],
            state=state,
            recommendation=recommendation,
            absolute_delta_fraction=delta_text,
            threshold=reference["threshold"],
            insufficiency_reason=insufficiency_reason,
            # evaluated_at is an admission upper bound only; persist the
            # immutable observation boundary for restart-idempotent findings.
            evaluated_at=current.as_of,
            evidence_sha256=evidence_sha256,
        )
        self.scientific_registry.append(finding)
        return finding

    def list_findings(self, *, as_of: str) -> tuple[RegistryEntry, ...]:
        return self.scientific_registry.causal_records("DriftFinding", as_of=as_of)

    @staticmethod
    def finding_binding(finding: DriftFinding) -> tuple[tuple[str, str], ...]:
        if not isinstance(finding, DriftFinding):
            raise TypeError("finding must be DriftFinding")
        return (("drift_finding_id", finding.finding_id),)
