"""Evidence-derived FAST/SLOW anchor-selection research contract.

This module sits above SportDomainFitnessObservation. It never changes route
authority, grants financial authority, or mutates the observation store.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Iterable, Mapping, Sequence

from .sport_domain_fitness import (
    EvidenceProvenance,
    EvidenceState,
    SportDomainFitnessError,
    SportDomainFitnessObservation,
)


_SCHEMA = "autosport.anchor_selection"
_VERSION = 1


class AnchorSelectionError(ValueError):
    """Raised when anchor-selection protocol/evidence is non-canonical."""


class AnchorDecisionState(StrEnum):
    CHECKPOINT = "CHECKPOINT"
    CONTINUE = "CONTINUE"
    INSUFFICIENT = "INSUFFICIENT"
    REJECT = "REJECT"
    SELECT = "SELECT"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"


_ANCHOR_METRICS = frozenset(
    {
        "catalogue_coverage",
        "quote_coverage",
        "recurrence_per_hour",
        "freshness_seconds",
        "reaction_slack_seconds",
        "executable_liquidity",
        "fee_fraction",
        "slippage_fraction",
        "capital_time_hours",
        "data_cost",
        "compute_cost",
        "compute_duration_seconds",
        "slow_analysis_deadline_seconds",
        "freshness_ttl_seconds",
        "calibration_error",
        "execution_feasibility",
        "settlement_identity_complexity",
        "oos_net_economic_value",
    }
)


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise AnchorSelectionError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8", errors="strict")
    return value


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnchorSelectionError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnchorSelectionError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _sha256(value: object, name: str) -> str:
    text = _text(name, value).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise AnchorSelectionError(f"{name} must be lowercase SHA-256")
    return text


def _decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AnchorSelectionError(f"{name} must be a finite Decimal")
    return value


def _positive_decimal(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result <= 0:
        raise AnchorSelectionError(f"{name} must be positive")
    return result


def _bounded_fraction(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result < 0 or result > 1:
        raise AnchorSelectionError(f"{name} must be between 0 and 1")
    return result


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _metric_value(observation: SportDomainFitnessObservation, name: str) -> Decimal | None:
    metric = getattr(observation, name)
    return metric.value if metric.state is EvidenceState.MEASURED else None


@dataclass(frozen=True, slots=True)
class AnchorSupplementalMetric:
    state: EvidenceState
    value: Decimal | None
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, EvidenceState):
            raise AnchorSelectionError("supplemental metric state must be EvidenceState")
        _text("unit", self.unit)
        if self.state is EvidenceState.MEASURED:
            if self.value is None or not isinstance(self.value, Decimal) or not self.value.is_finite():
                raise AnchorSelectionError("MEASURED supplemental metric requires finite Decimal")
        elif self.value is not None:
            raise AnchorSelectionError("unmeasured supplemental metric must not carry a value")


@dataclass(frozen=True, slots=True)
class AnchorSupplementalEvidence:
    observation_id: str
    evidence_sha256: str
    calibration_error: AnchorSupplementalMetric
    execution_feasibility: AnchorSupplementalMetric
    settlement_identity_complexity: AnchorSupplementalMetric
    oos_net_economic_value: AnchorSupplementalMetric

    def __post_init__(self) -> None:
        _text("observation_id", self.observation_id)
        _sha256(self.evidence_sha256, "evidence_sha256")
        for name, unit in {
            "calibration_error": "fraction",
            "execution_feasibility": "fraction",
            "settlement_identity_complexity": "complexity",
            "oos_net_economic_value": "net_economic_value",
        }.items():
            metric = getattr(self, name)
            if not isinstance(metric, AnchorSupplementalMetric) or metric.unit != unit:
                raise AnchorSelectionError(f"{name} must use unit {unit!r}")
        for name in ("calibration_error", "execution_feasibility"):
            metric = getattr(self, name)
            if metric.state is EvidenceState.MEASURED and not Decimal("0") <= metric.value <= Decimal("1"):
                raise AnchorSelectionError(f"{name} must be between 0 and 1")

    def metric_value(self, name: str) -> Decimal | None:
        metric = getattr(self, name)
        return metric.value if metric.state is EvidenceState.MEASURED else None


@dataclass(frozen=True, slots=True)
class AnchorMetricRule:
    name: str
    direction: MetricDirection
    weight: Decimal
    scale: Decimal

    def __post_init__(self) -> None:
        if self.name not in _ANCHOR_METRICS:
            raise AnchorSelectionError(f"unsupported anchor metric {self.name!r}")
        if not isinstance(self.direction, MetricDirection):
            raise AnchorSelectionError("direction must be MetricDirection")
        _positive_decimal("weight", self.weight)
        _positive_decimal("scale", self.scale)

    def payload(self) -> dict[str, str]:
        return {
            "name": self.name,
            "direction": self.direction.value,
            "weight": str(self.weight),
            "scale": str(self.scale),
        }


_DEFAULT_RULES = (
    AnchorMetricRule("catalogue_coverage", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("1")),
    AnchorMetricRule("quote_coverage", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("1")),
    AnchorMetricRule("recurrence_per_hour", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("10")),
    AnchorMetricRule("freshness_seconds", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("10")),
    AnchorMetricRule("reaction_slack_seconds", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("10")),
    AnchorMetricRule("executable_liquidity", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("100")),
    AnchorMetricRule("fee_fraction", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("0.05")),
    AnchorMetricRule("slippage_fraction", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("0.05")),
    AnchorMetricRule("capital_time_hours", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("24")),
    AnchorMetricRule("data_cost", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("10")),
    AnchorMetricRule("compute_cost", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("10")),
    AnchorMetricRule("compute_duration_seconds", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("10")),
    AnchorMetricRule("slow_analysis_deadline_seconds", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("10")),
    AnchorMetricRule("freshness_ttl_seconds", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("30")),
    AnchorMetricRule("calibration_error", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("0.25")),
    AnchorMetricRule("execution_feasibility", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("1")),
    AnchorMetricRule("settlement_identity_complexity", MetricDirection.LOWER_IS_BETTER, Decimal("1"), Decimal("5")),
    AnchorMetricRule("oos_net_economic_value", MetricDirection.HIGHER_IS_BETTER, Decimal("1"), Decimal("1")),
)


@dataclass(frozen=True, slots=True)
class AnchorSelectionProtocol:
    experiment_id: str
    candidate_sports: tuple[str, ...]
    measurement_start: str
    measurement_end: str
    decision_as_of: str
    minimum_observations: int = 3
    minimum_effective_sample_size: int = 2
    max_missing_fraction: Decimal = Decimal("0.20")
    minimum_score: Decimal = Decimal("0.50")
    minimum_score_separation: Decimal = Decimal("0.05")
    metrics: tuple[AnchorMetricRule, ...] = _DEFAULT_RULES
    protocol_version: str = "v1"

    def __post_init__(self) -> None:
        _text("experiment_id", self.experiment_id)
        if not self.candidate_sports:
            raise AnchorSelectionError("candidate_sports must not be empty")
        candidate_sports = tuple(_text("candidate_sport", item) for item in self.candidate_sports)
        if candidate_sports != tuple(sorted(set(candidate_sports))):
            raise AnchorSelectionError("candidate_sports must be sorted and unique")
        start = _instant("measurement_start", self.measurement_start)
        end = _instant("measurement_end", self.measurement_end)
        as_of = _instant("decision_as_of", self.decision_as_of)
        if end <= start:
            raise AnchorSelectionError("measurement_end must follow measurement_start")
        if as_of < start:
            raise AnchorSelectionError("decision_as_of precedes measurement_start")
        if isinstance(self.minimum_observations, bool) or self.minimum_observations <= 0:
            raise AnchorSelectionError("minimum_observations must be positive")
        if isinstance(self.minimum_effective_sample_size, bool) or self.minimum_effective_sample_size <= 0:
            raise AnchorSelectionError("minimum_effective_sample_size must be positive")
        _bounded_fraction("max_missing_fraction", self.max_missing_fraction)
        _bounded_fraction("minimum_score", self.minimum_score)
        _bounded_fraction("minimum_score_separation", self.minimum_score_separation)
        _text("protocol_version", self.protocol_version)
        if not self.metrics:
            raise AnchorSelectionError("metrics must not be empty")
        names = [rule.name for rule in self.metrics]
        if len(names) != len(set(names)):
            raise AnchorSelectionError("anchor metrics must be unique")

    @property
    def measurement_window_seconds(self) -> int:
        return int((_instant("measurement_end", self.measurement_end) - _instant("measurement_start", self.measurement_start)).total_seconds())

    def payload(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "version": _VERSION,
            "experiment_id": self.experiment_id,
            "candidate_sports": list(self.candidate_sports),
            "measurement_start": _time("measurement_start", self.measurement_start),
            "measurement_end": _time("measurement_end", self.measurement_end),
            "decision_as_of": _time("decision_as_of", self.decision_as_of),
            "minimum_observations": self.minimum_observations,
            "minimum_effective_sample_size": self.minimum_effective_sample_size,
            "max_missing_fraction": str(self.max_missing_fraction),
            "minimum_score": str(self.minimum_score),
            "minimum_score_separation": str(self.minimum_score_separation),
            "metrics": [rule.payload() for rule in self.metrics],
            "protocol_version": self.protocol_version,
        }

    @property
    def protocol_sha256(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class AnchorCandidateAggregate:
    sport_id: str
    observation_ids: tuple[str, ...]
    dependence_keys: tuple[str, ...]
    raw_observation_count: int
    effective_sample_size: int
    missing_fraction: Decimal
    metric_means: Mapping[str, Decimal | None]
    metric_interval_low: Mapping[str, Decimal | None]
    metric_interval_high: Mapping[str, Decimal | None]
    score: Decimal | None
    score_low: Decimal | None
    score_high: Decimal | None

    def __post_init__(self) -> None:
        _text("sport_id", self.sport_id)
        if self.raw_observation_count < 0 or self.effective_sample_size < 0:
            raise AnchorSelectionError("sample sizes must be non-negative")
        _bounded_fraction("missing_fraction", self.missing_fraction)
        for name, value in self.metric_means.items():
            if value is not None:
                _decimal(f"metric_means.{name}", value)
        for value in (*self.metric_interval_low.values(), *self.metric_interval_high.values()):
            if value is not None:
                _decimal("metric interval", value)
        for name, value in (("score", self.score), ("score_low", self.score_low), ("score_high", self.score_high)):
            if value is not None:
                _bounded_fraction(name, value)
        if self.score_low is not None and self.score_high is not None and self.score_low > self.score_high:
            raise AnchorSelectionError("score interval is inverted")

    def payload(self) -> dict[str, object]:
        def serialize(values: Mapping[str, Decimal | None]) -> dict[str, str | None]:
            return {key: None if value is None else str(value) for key, value in sorted(values.items())}
        return {
            "sport_id": self.sport_id,
            "observation_ids": list(self.observation_ids),
            "dependence_keys": list(self.dependence_keys),
            "raw_observation_count": self.raw_observation_count,
            "effective_sample_size": self.effective_sample_size,
            "missing_fraction": str(self.missing_fraction),
            "metric_means": serialize(self.metric_means),
            "metric_interval_low": serialize(self.metric_interval_low),
            "metric_interval_high": serialize(self.metric_interval_high),
            "score": None if self.score is None else str(self.score),
            "score_low": None if self.score_low is None else str(self.score_low),
            "score_high": None if self.score_high is None else str(self.score_high),
        }


@dataclass(frozen=True, slots=True)
class AnchorSelectionReport:
    protocol_sha256: str
    decision_state: AnchorDecisionState
    selected_sport_id: str | None
    candidate_reports: tuple[AnchorCandidateAggregate, ...]
    input_observation_ids: tuple[str, ...]
    rationale: str

    def __post_init__(self) -> None:
        _sha256(self.protocol_sha256, "protocol_sha256")
        if not isinstance(self.decision_state, AnchorDecisionState):
            raise AnchorSelectionError("decision_state must be AnchorDecisionState")
        if self.selected_sport_id is not None:
            _text("selected_sport_id", self.selected_sport_id)
            if self.selected_sport_id not in {item.sport_id for item in self.candidate_reports}:
                raise AnchorSelectionError("selected_sport_id must exist in candidate_reports")
        _text("rationale", self.rationale)

    def payload(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "version": _VERSION,
            "protocol_sha256": self.protocol_sha256,
            "decision_state": self.decision_state.value,
            "selected_sport_id": self.selected_sport_id,
            "candidate_reports": [item.payload() for item in self.candidate_reports],
            "input_observation_ids": list(self.input_observation_ids),
            "rationale": self.rationale,
        }

    @property
    def report_sha256(self) -> str:
        return _digest(self.payload())


def _dependence_key(observation: SportDomainFitnessObservation) -> str:
    return "|".join(
        (
            observation.sport_id,
            observation.league_id,
            observation.market_id,
            observation.provider_id,
        )
    )


def _cluster_mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _confidence_interval(values: Sequence[Decimal], *, positive: bool = False) -> tuple[Decimal, Decimal]:
    mean = _cluster_mean(values)
    if len(values) < 2:
        return mean, mean
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values) - 1)
    stddev = Decimal(str(math.sqrt(float(variance))))
    half = Decimal("1.96") * stddev / Decimal(str(math.sqrt(len(values))))
    low = mean - half
    high = mean + half
    if positive:
        low = max(Decimal("0"), low)
    return low, high


def _normalized(value: Decimal, rule: AnchorMetricRule) -> Decimal:
    if rule.name == "oos_net_economic_value":
        magnitude = abs(value)
        return Decimal("0.5") + (value / (magnitude + rule.scale)) / Decimal("2")
    ratio = value / (value + rule.scale)
    return ratio if rule.direction is MetricDirection.HIGHER_IS_BETTER else Decimal("1") - ratio


def _weighted_score(values: Mapping[str, Decimal | None], rules: Sequence[AnchorMetricRule], missing_fraction: Decimal) -> Decimal:
    numerator = Decimal("0")
    denominator = Decimal("0")
    for rule in rules:
        value = values[rule.name]
        if value is None:
            continue
        numerator += rule.weight * _normalized(value, rule)
        denominator += rule.weight
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator * (Decimal("1") - missing_fraction)


def _score_interval(
    means: Mapping[str, Decimal | None],
    lows: Mapping[str, Decimal | None],
    highs: Mapping[str, Decimal | None],
    rules: Sequence[AnchorMetricRule],
    missing_fraction: Decimal,
) -> tuple[Decimal, Decimal]:
    low_values: dict[str, Decimal | None] = {}
    high_values: dict[str, Decimal | None] = {}
    for rule in rules:
        mean = means[rule.name]
        low = lows[rule.name]
        high = highs[rule.name]
        if mean is None or low is None or high is None:
            low_values[rule.name] = None
            high_values[rule.name] = None
        elif rule.direction is MetricDirection.HIGHER_IS_BETTER:
            low_values[rule.name] = max(Decimal("0"), low)
            high_values[rule.name] = max(Decimal("0"), high)
        else:
            low_values[rule.name] = max(Decimal("0"), low)
            high_values[rule.name] = max(Decimal("0"), high)
    score_low = _weighted_score(
        {rule.name: (high_values[rule.name] if rule.direction is MetricDirection.LOWER_IS_BETTER else low_values[rule.name]) for rule in rules},
        rules,
        missing_fraction,
    )
    score_high = _weighted_score(
        {rule.name: (low_values[rule.name] if rule.direction is MetricDirection.LOWER_IS_BETTER else high_values[rule.name]) for rule in rules},
        rules,
        missing_fraction,
    )
    return max(Decimal("0"), score_low), min(Decimal("1"), score_high)


def _aggregate_candidate(
    sport_id: str,
    observations: Sequence[SportDomainFitnessObservation],
    protocol: AnchorSelectionProtocol,
    supplemental_by_observation: Mapping[str, AnchorSupplementalEvidence],
) -> AnchorCandidateAggregate:
    by_cluster: dict[str, list[SportDomainFitnessObservation]] = {}
    seen_identity: set[tuple[str, ...]] = set()
    kept: list[SportDomainFitnessObservation] = []
    for observation in observations:
        identity = (
            observation.observation_id,
            observation.evidence_sha256,
            observation.measured_from,
            observation.measured_until,
        )
        if identity in seen_identity:
            continue
        seen_identity.add(identity)
        cluster = _dependence_key(observation)
        by_cluster.setdefault(cluster, []).append(observation)
        kept.append(observation)

    raw_count = len(kept)
    effective_n = len(by_cluster)
    metric_means: dict[str, Decimal | None] = {}
    metric_low: dict[str, Decimal | None] = {}
    metric_high: dict[str, Decimal | None] = {}
    missing_values = 0
    total_values = 0

    for rule in protocol.metrics:
        cluster_values: list[Decimal] = []
        for cluster_observations in by_cluster.values():
            measured = []
            for item in cluster_observations:
                if rule.name in {
                    "calibration_error",
                    "execution_feasibility",
                    "settlement_identity_complexity",
                    "oos_net_economic_value",
                }:
                    evidence = supplemental_by_observation.get(item.observation_id)
                    value = None if evidence is None else evidence.metric_value(rule.name)
                else:
                    value = _metric_value(item, rule.name)
                if value is not None:
                    measured.append(value)
            if measured:
                cluster_values.append(_cluster_mean(measured))
        total_values += effective_n
        missing_values += max(0, effective_n - len(cluster_values))
        if cluster_values:
            mean = _cluster_mean(cluster_values)
            low, high = _confidence_interval(cluster_values, positive=True)
            metric_means[rule.name] = mean
            metric_low[rule.name] = low
            metric_high[rule.name] = high
        else:
            metric_means[rule.name] = None
            metric_low[rule.name] = None
            metric_high[rule.name] = None

    missing_fraction = (
        Decimal(missing_values) / Decimal(total_values)
        if total_values
        else Decimal("1")
    )
    score = _weighted_score(metric_means, protocol.metrics, missing_fraction)
    score_low, score_high = _score_interval(
        metric_means, metric_low, metric_high, protocol.metrics, missing_fraction
    )

    return AnchorCandidateAggregate(
        sport_id=sport_id,
        observation_ids=tuple(sorted(item.observation_id for item in kept)),
        dependence_keys=tuple(sorted(by_cluster)),
        raw_observation_count=raw_count,
        effective_sample_size=effective_n,
        missing_fraction=missing_fraction,
        metric_means=metric_means,
        metric_interval_low=metric_low,
        metric_interval_high=metric_high,
        score=score,
        score_low=score_low,
        score_high=score_high,
    )


def evaluate_anchor_selection(
    observations: Iterable[SportDomainFitnessObservation],
    protocol: AnchorSelectionProtocol,
    supplemental_evidence: Iterable[AnchorSupplementalEvidence] = (),
) -> AnchorSelectionReport:
    """Evaluate a frozen candidate universe without mutating source evidence."""
    if not isinstance(protocol, AnchorSelectionProtocol):
        raise TypeError("protocol must be AnchorSelectionProtocol")

    start = _instant("measurement_start", protocol.measurement_start)
    end = _instant("measurement_end", protocol.measurement_end)
    as_of = _instant("decision_as_of", protocol.decision_as_of)
    observed_inputs = tuple(observations)
    by_id = {item.observation_id: item for item in observed_inputs}
    if len(by_id) != len(observed_inputs):
        raise AnchorSelectionError("observation ids must be unique")

    supplemental_by_observation: dict[str, AnchorSupplementalEvidence] = {}
    for evidence in supplemental_evidence:
        if not isinstance(evidence, AnchorSupplementalEvidence):
            raise TypeError("supplemental_evidence must contain AnchorSupplementalEvidence")
        if evidence.observation_id in supplemental_by_observation:
            raise AnchorSelectionError("supplemental evidence ids must be unique")
        observation = by_id.get(evidence.observation_id)
        if observation is None:
            raise AnchorSelectionError("supplemental evidence references an unknown observation")
        if observation.evidence_sha256 != evidence.evidence_sha256:
            raise AnchorSelectionError("supplemental evidence does not bind the observation evidence hash")
        supplemental_by_observation[evidence.observation_id] = evidence

    candidates: dict[str, list[SportDomainFitnessObservation]] = {
        sport_id: [] for sport_id in protocol.candidate_sports
    }
    input_ids: set[str] = set()

    for observation in observed_inputs:
        if not isinstance(observation, SportDomainFitnessObservation):
            raise TypeError("observations must contain SportDomainFitnessObservation")
        if observation.provenance is not EvidenceProvenance.OBSERVED:
            continue
        observed_start = _instant("measured_from", observation.measured_from)
        observed_end = _instant("measured_until", observation.measured_until)
        available = _instant("available_at", observation.available_at)
        if observation.sport_id not in candidates:
            continue
        if observed_start < start or observed_end > end:
            continue
        if observed_end > as_of or available >= as_of:
            continue
        candidates[observation.sport_id].append(observation)
        input_ids.add(observation.observation_id)

    reports = tuple(
        _aggregate_candidate(
            sport_id,
            tuple(candidates[sport_id]),
            protocol,
            supplemental_by_observation,
        )
        for sport_id in protocol.candidate_sports
    )

    # A pre-14-day run is a measurement checkpoint, not a final evidence
    # sufficiency/selection decision. Preserve the checkpoint state even when
    # the partial window cannot yet satisfy the frozen minimums.
    elapsed = as_of - start
    if elapsed < timedelta(days=14):
        return AnchorSelectionReport(
            protocol.protocol_sha256,
            AnchorDecisionState.CHECKPOINT,
            None,
            reports,
            tuple(sorted(input_ids)),
            "measurement window is shorter than the fixed 14-day review horizon; report evidence but do not select an anchor",
        )

    insufficient = [
        report
        for report in reports
        if (
            report.raw_observation_count < protocol.minimum_observations
            or report.effective_sample_size < protocol.minimum_effective_sample_size
            or report.score is None
            or report.missing_fraction > protocol.max_missing_fraction
        )
    ]
    if insufficient:
        return AnchorSelectionReport(
            protocol.protocol_sha256,
            AnchorDecisionState.INSUFFICIENT,
            None,
            reports,
            tuple(sorted(input_ids)),
            "one or more frozen candidates lack the minimum independent evidence or exceed the missing-evidence allowance",
        )

    if all(report.score is not None and report.score < protocol.minimum_score for report in reports):
        return AnchorSelectionReport(
            protocol.protocol_sha256,
            AnchorDecisionState.REJECT,
            None,
            reports,
            tuple(sorted(input_ids)),
            "all candidates remain below the frozen minimum anchor-selection score",
        )

    ordered = sorted(
        reports,
        key=lambda report: (
            Decimal("-1") if report.score is None else -report.score,
            report.sport_id,
        ),
    )
    leader = ordered[0]
    runner_up = ordered[1]
    elapsed = _instant("decision_as_of", protocol.decision_as_of) - start
    if elapsed < timedelta(days=14):
        state = AnchorDecisionState.CHECKPOINT
        selected = None
        rationale = "measurement window is shorter than the fixed 14-day review horizon; report evidence but do not select an anchor"
    elif (
        leader.score_low is not None
        and runner_up.score_high is not None
        and leader.score_low
        >= runner_up.score_high + protocol.minimum_score_separation
    ):
        state = AnchorDecisionState.SELECT
        selected = leader.sport_id
        rationale = "leader's conservative score bound exceeds the runner-up bound by the frozen separation margin"
    else:
        state = AnchorDecisionState.CONTINUE
        selected = None
        rationale = "sufficient evidence exists, but uncertainty or score separation does not justify a reproducible selection"

    return AnchorSelectionReport(
        protocol.protocol_sha256,
        state,
        selected,
        reports,
        tuple(sorted(input_ids)),
        rationale,
    )
