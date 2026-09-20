"""Frozen external-validity baseline comparison authority.

This module is deliberately small and deterministic. It does not own model
training, scientific promotion, provider execution, portfolio money arithmetic,
or causal feature availability. It freezes the evidence surface on which a
candidate and simple baselines are evaluated and refuses comparisons that do
not bind exactly the same corpus/cohort/cutoff/cost/execution evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Sequence


class ExternalValidityError(ValueError):
    """Raised when external-validity comparison evidence is inconsistent."""


class BaselineKind(str, Enum):
    """Baseline families required by the frozen comparison protocol."""

    NO_BET_WAIT = "no-bet-wait"
    MARKET_IMPLIED_DEVIG = "market-implied-devig"
    PARTICIPANT_STRENGTH = "participant-strength-rating"
    CALIBRATED_STATISTICAL = "calibrated-statistical-challenger"
    FIXED_SELECTION_THRESHOLD = "fixed-selection-threshold"
    FIXED_FRACTIONAL_STAKING = "fixed-fractional-staking"
    EQUAL_WEIGHT_PORTFOLIO = "equal-weight-candidate-portfolio"


REQUIRED_BASELINE_KINDS: tuple[BaselineKind, ...] = tuple(BaselineKind)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ExternalValidityError(f"{field} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ExternalValidityError(f"{field} must be canonical SHA-256 hex")
    return text


def _instant(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalValidityError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExternalValidityError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ExternalValidityError(f"{field} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExternalValidityError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ExternalValidityError(f"{field} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ExternalValidityError("decimal must be finite")
    text = format(value.normalize(), "f")
    return "0" if text in ("", "-0") else text


def _digest(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenEvidenceScope:
    """Exact evidence universe shared by every policy in one comparison."""

    dataset_sha256: str
    dataset_cutoff: str
    cohort_keys: tuple[str, ...]
    market_evidence_sha256: str
    outcome_evidence_sha256: str
    cost_model_sha256: str
    execution_model_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "dataset_sha256",
            "market_evidence_sha256",
            "outcome_evidence_sha256",
            "cost_model_sha256",
            "execution_model_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        _instant(self.dataset_cutoff, "dataset_cutoff")
        if type(self.cohort_keys) is not tuple or not self.cohort_keys:
            raise ExternalValidityError("cohort_keys must be a non-empty canonical tuple")
        for key in self.cohort_keys:
            _text(key, "cohort key")
        if len(set(self.cohort_keys)) != len(self.cohort_keys):
            raise ExternalValidityError("cohort_keys must be unique")
        if self.cohort_keys != tuple(sorted(self.cohort_keys)):
            raise ExternalValidityError("cohort_keys must use canonical sorted order")

    @property
    def sample_count(self) -> int:
        return len(self.cohort_keys)

    @property
    def cohort_sha256(self) -> str:
        return _digest({"cohort_keys": list(self.cohort_keys)})

    @property
    def identity_sha256(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "dataset_sha256": self.dataset_sha256,
            "dataset_cutoff": self.dataset_cutoff,
            "cohort_keys": list(self.cohort_keys),
            "cohort_sha256": self.cohort_sha256,
            "sample_count": self.sample_count,
            "market_evidence_sha256": self.market_evidence_sha256,
            "outcome_evidence_sha256": self.outcome_evidence_sha256,
            "cost_model_sha256": self.cost_model_sha256,
            "execution_model_sha256": self.execution_model_sha256,
        }


@dataclass(frozen=True, slots=True)
class BaselineDefinition:
    """Frozen identity/configuration of one required simple baseline."""

    kind: BaselineKind
    baseline_id: str
    implementation_sha256: str
    config_sha256: str
    supported: bool
    unsupported_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, BaselineKind):
            raise ExternalValidityError("baseline kind must be a BaselineKind")
        _text(self.baseline_id, "baseline_id")
        object.__setattr__(
            self,
            "implementation_sha256",
            _sha256(self.implementation_sha256, "implementation_sha256"),
        )
        object.__setattr__(
            self,
            "config_sha256",
            _sha256(self.config_sha256, "config_sha256"),
        )
        if type(self.supported) is not bool:
            raise ExternalValidityError("supported must be boolean")
        if self.supported:
            if self.unsupported_reason is not None:
                raise ExternalValidityError(
                    "supported baseline must not carry unsupported_reason"
                )
        else:
            _text(self.unsupported_reason, "unsupported_reason")

    @property
    def definition_sha256(self) -> str:
        return _digest(self.to_payload(include_identity=False))

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind.value,
            "baseline_id": self.baseline_id,
            "implementation_sha256": self.implementation_sha256,
            "config_sha256": self.config_sha256,
            "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
        }
        if include_identity:
            payload["definition_sha256"] = self.definition_sha256
        return payload


@dataclass(frozen=True, slots=True)
class FrozenBaselineProtocol:
    """Versioned comparison protocol frozen before result consumption."""

    protocol_id: str
    frozen_at: str
    evidence_scope: FrozenEvidenceScope
    candidate_id: str
    candidate_artifact_sha256: str
    evaluation_semantics: str
    primary_metric: str
    uncertainty_method: str
    baselines: tuple[BaselineDefinition, ...]

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        _text(self.protocol_id, "protocol_id")
        _instant(self.frozen_at, "frozen_at")
        if not isinstance(self.evidence_scope, FrozenEvidenceScope):
            raise ExternalValidityError("evidence_scope must be FrozenEvidenceScope")
        _text(self.candidate_id, "candidate_id")
        object.__setattr__(
            self,
            "candidate_artifact_sha256",
            _sha256(self.candidate_artifact_sha256, "candidate_artifact_sha256"),
        )
        _text(self.evaluation_semantics, "evaluation_semantics")
        _text(self.primary_metric, "primary_metric")
        _text(self.uncertainty_method, "uncertainty_method")
        if type(self.baselines) is not tuple:
            raise ExternalValidityError("baselines must be a canonical tuple")
        if any(not isinstance(item, BaselineDefinition) for item in self.baselines):
            raise ExternalValidityError("baselines must contain BaselineDefinition values")
        kinds = tuple(item.kind for item in self.baselines)
        if kinds != REQUIRED_BASELINE_KINDS:
            raise ExternalValidityError(
                "baselines must contain every required kind exactly once in canonical order"
            )
        ids = [item.baseline_id for item in self.baselines]
        if len(ids) != len(set(ids)):
            raise ExternalValidityError("baseline_id values must be unique")
        if self.candidate_id in ids:
            raise ExternalValidityError("candidate_id must be distinct from baseline_id values")

    @property
    def identity_sha256(self) -> str:
        return _digest(self.to_payload(include_identity=False))

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.SCHEMA_VERSION,
            "kind": "autosport_frozen_external_validity_protocol",
            "protocol_id": self.protocol_id,
            "frozen_at": self.frozen_at,
            "evidence_scope": self.evidence_scope.to_payload(),
            "evidence_scope_sha256": self.evidence_scope.identity_sha256,
            "candidate_id": self.candidate_id,
            "candidate_artifact_sha256": self.candidate_artifact_sha256,
            "evaluation_semantics": self.evaluation_semantics,
            "primary_metric": self.primary_metric,
            "uncertainty_method": self.uncertainty_method,
            "baselines": [item.to_payload() for item in self.baselines],
        }
        if include_identity:
            payload["protocol_sha256"] = self.identity_sha256
        return payload


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """Result evidence for one candidate or supported baseline on the frozen scope."""

    policy_id: str
    policy_artifact_sha256: str
    protocol_sha256: str
    evidence_scope_sha256: str
    cohort_sha256: str
    primary_metric: str
    metric_value: str
    uncertainty_low: str
    uncertainty_high: str
    observed_count: int
    scored_count: int
    abstention_count: int
    total_cost: str
    evaluation_bundle_sha256: str
    baseline_definition_sha256: str | None = None

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        for field in (
            "policy_artifact_sha256",
            "protocol_sha256",
            "evidence_scope_sha256",
            "cohort_sha256",
            "evaluation_bundle_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        if self.baseline_definition_sha256 is not None:
            object.__setattr__(
                self,
                "baseline_definition_sha256",
                _sha256(
                    self.baseline_definition_sha256,
                    "baseline_definition_sha256",
                ),
            )
        _text(self.primary_metric, "primary_metric")
        value = _decimal(self.metric_value, "metric_value")
        low = _decimal(self.uncertainty_low, "uncertainty_low")
        high = _decimal(self.uncertainty_high, "uncertainty_high")
        if low > value or value > high:
            raise ExternalValidityError(
                "metric_value must lie inside uncertainty_low..uncertainty_high"
            )
        cost = _decimal(self.total_cost, "total_cost")
        if cost < 0:
            raise ExternalValidityError("total_cost must be non-negative")
        for field in ("observed_count", "scored_count", "abstention_count"):
            value_int = getattr(self, field)
            if type(value_int) is not int or value_int < 0:
                raise ExternalValidityError(f"{field} must be a non-negative integer")
        if self.scored_count + self.abstention_count != self.observed_count:
            raise ExternalValidityError(
                "scored_count + abstention_count must equal observed_count"
            )
        object.__setattr__(self, "metric_value", _decimal_text(value))
        object.__setattr__(self, "uncertainty_low", _decimal_text(low))
        object.__setattr__(self, "uncertainty_high", _decimal_text(high))
        object.__setattr__(self, "total_cost", _decimal_text(cost))

    @property
    def identity_sha256(self) -> str:
        return _digest(self.to_payload(include_identity=False))

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "policy_id": self.policy_id,
            "policy_artifact_sha256": self.policy_artifact_sha256,
            "protocol_sha256": self.protocol_sha256,
            "evidence_scope_sha256": self.evidence_scope_sha256,
            "cohort_sha256": self.cohort_sha256,
            "primary_metric": self.primary_metric,
            "metric_value": self.metric_value,
            "uncertainty_low": self.uncertainty_low,
            "uncertainty_high": self.uncertainty_high,
            "observed_count": self.observed_count,
            "scored_count": self.scored_count,
            "abstention_count": self.abstention_count,
            "total_cost": self.total_cost,
            "evaluation_bundle_sha256": self.evaluation_bundle_sha256,
            "baseline_definition_sha256": self.baseline_definition_sha256,
        }
        if include_identity:
            payload["evaluation_sha256"] = self.identity_sha256
        return payload


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    kind: BaselineKind
    baseline_id: str
    supported: bool
    unsupported_reason: str | None
    baseline_definition_sha256: str
    candidate_metric_value: str | None = None
    baseline_metric_value: str | None = None
    candidate_minus_baseline: str | None = None
    difference_lower_bound: str | None = None
    difference_upper_bound: str | None = None
    candidate_abstention_count: int | None = None
    baseline_abstention_count: int | None = None
    abstention_delta: int | None = None
    candidate_total_cost: str | None = None
    baseline_total_cost: str | None = None
    total_cost_delta: str | None = None
    baseline_evaluation_sha256: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "baseline_id": self.baseline_id,
            "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
            "baseline_definition_sha256": self.baseline_definition_sha256,
            "candidate_metric_value": self.candidate_metric_value,
            "baseline_metric_value": self.baseline_metric_value,
            "candidate_minus_baseline": self.candidate_minus_baseline,
            "difference_lower_bound": self.difference_lower_bound,
            "difference_upper_bound": self.difference_upper_bound,
            "uncertainty_bound_method": (
                "arm-interval-difference-bound-not-paired-ci" if self.supported else None
            ),
            "candidate_abstention_count": self.candidate_abstention_count,
            "baseline_abstention_count": self.baseline_abstention_count,
            "abstention_delta": self.abstention_delta,
            "candidate_total_cost": self.candidate_total_cost,
            "baseline_total_cost": self.baseline_total_cost,
            "total_cost_delta": self.total_cost_delta,
            "baseline_evaluation_sha256": self.baseline_evaluation_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExternalValidityReport:
    protocol_sha256: str
    evidence_scope_sha256: str
    candidate_evaluation_sha256: str
    comparisons: tuple[BaselineComparison, ...]

    SCHEMA_VERSION = 1

    @property
    def identity_sha256(self) -> str:
        return _digest(self.to_payload(include_identity=False))

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.SCHEMA_VERSION,
            "kind": "autosport_external_validity_baseline_report",
            "protocol_sha256": self.protocol_sha256,
            "evidence_scope_sha256": self.evidence_scope_sha256,
            "candidate_evaluation_sha256": self.candidate_evaluation_sha256,
            "comparisons": [item.to_payload() for item in self.comparisons],
            "truth": {
                "same_frozen_evidence_scope": True,
                "unsupported_baselines_explicit": True,
                "winner_selected": False,
                "ranking_produced": False,
                "promotion_authority": False,
                "real_money_execution": False,
            },
        }
        if include_identity:
            payload["report_sha256"] = self.identity_sha256
        return payload


def _validate_common_result(
    protocol: FrozenBaselineProtocol,
    result: PolicyEvaluation,
) -> None:
    if result.protocol_sha256 != protocol.identity_sha256:
        raise ExternalValidityError(
            f"{result.policy_id}: result protocol does not match frozen protocol"
        )
    scope = protocol.evidence_scope
    if result.evidence_scope_sha256 != scope.identity_sha256:
        raise ExternalValidityError(
            f"{result.policy_id}: result evidence scope does not match frozen scope"
        )
    if result.cohort_sha256 != scope.cohort_sha256:
        raise ExternalValidityError(
            f"{result.policy_id}: result cohort does not match frozen cohort"
        )
    if result.observed_count != scope.sample_count:
        raise ExternalValidityError(
            f"{result.policy_id}: result did not observe the complete frozen cohort"
        )
    if result.primary_metric != protocol.primary_metric:
        raise ExternalValidityError(
            f"{result.policy_id}: primary metric differs from frozen protocol"
        )


def build_external_validity_report(
    protocol: FrozenBaselineProtocol,
    candidate: PolicyEvaluation,
    baseline_results: Sequence[PolicyEvaluation],
) -> ExternalValidityReport:
    """Validate and compare one candidate against every supported frozen baseline.

    The function deliberately emits no winner, ordering, promotion decision, or
    significance claim. The difference interval is only the arithmetic bound
    induced by the two supplied arm intervals; it is not represented as a
    paired confidence interval.
    """

    if not isinstance(protocol, FrozenBaselineProtocol):
        raise ExternalValidityError("protocol must be FrozenBaselineProtocol")
    if not isinstance(candidate, PolicyEvaluation):
        raise ExternalValidityError("candidate must be PolicyEvaluation")
    _validate_common_result(protocol, candidate)
    if candidate.policy_id != protocol.candidate_id:
        raise ExternalValidityError("candidate result policy_id mismatch")
    if candidate.policy_artifact_sha256 != protocol.candidate_artifact_sha256:
        raise ExternalValidityError("candidate artifact changed after protocol freeze")
    if candidate.baseline_definition_sha256 is not None:
        raise ExternalValidityError("candidate must not claim a baseline definition")

    by_id: dict[str, PolicyEvaluation] = {}
    for result in baseline_results:
        if not isinstance(result, PolicyEvaluation):
            raise ExternalValidityError(
                "baseline_results must contain PolicyEvaluation values"
            )
        if result.policy_id in by_id:
            raise ExternalValidityError(
                f"duplicate baseline result for policy_id: {result.policy_id}"
            )
        by_id[result.policy_id] = result

    known_ids = {item.baseline_id for item in protocol.baselines}
    unexpected = sorted(set(by_id) - known_ids)
    if unexpected:
        raise ExternalValidityError(
            "baseline result is not present in frozen protocol: " + ",".join(unexpected)
        )

    comparisons: list[BaselineComparison] = []
    candidate_value = _decimal(candidate.metric_value, "candidate metric_value")
    candidate_low = _decimal(candidate.uncertainty_low, "candidate uncertainty_low")
    candidate_high = _decimal(candidate.uncertainty_high, "candidate uncertainty_high")
    candidate_cost = _decimal(candidate.total_cost, "candidate total_cost")

    for definition in protocol.baselines:
        result = by_id.get(definition.baseline_id)
        if not definition.supported:
            if result is not None:
                raise ExternalValidityError(
                    f"unsupported baseline must not carry fabricated result: {definition.baseline_id}"
                )
            comparisons.append(
                BaselineComparison(
                    kind=definition.kind,
                    baseline_id=definition.baseline_id,
                    supported=False,
                    unsupported_reason=definition.unsupported_reason,
                    baseline_definition_sha256=definition.definition_sha256,
                )
            )
            continue
        if result is None:
            raise ExternalValidityError(
                f"supported baseline result is missing: {definition.baseline_id}"
            )
        _validate_common_result(protocol, result)
        if result.policy_artifact_sha256 != definition.implementation_sha256:
            raise ExternalValidityError(
                f"{definition.baseline_id}: baseline implementation changed after freeze"
            )
        if result.baseline_definition_sha256 != definition.definition_sha256:
            raise ExternalValidityError(
                f"{definition.baseline_id}: baseline definition changed after freeze"
            )

        baseline_value = _decimal(result.metric_value, "baseline metric_value")
        baseline_low = _decimal(result.uncertainty_low, "baseline uncertainty_low")
        baseline_high = _decimal(result.uncertainty_high, "baseline uncertainty_high")
        baseline_cost = _decimal(result.total_cost, "baseline total_cost")
        comparisons.append(
            BaselineComparison(
                kind=definition.kind,
                baseline_id=definition.baseline_id,
                supported=True,
                unsupported_reason=None,
                baseline_definition_sha256=definition.definition_sha256,
                candidate_metric_value=candidate.metric_value,
                baseline_metric_value=result.metric_value,
                candidate_minus_baseline=_decimal_text(candidate_value - baseline_value),
                difference_lower_bound=_decimal_text(candidate_low - baseline_high),
                difference_upper_bound=_decimal_text(candidate_high - baseline_low),
                candidate_abstention_count=candidate.abstention_count,
                baseline_abstention_count=result.abstention_count,
                abstention_delta=candidate.abstention_count - result.abstention_count,
                candidate_total_cost=candidate.total_cost,
                baseline_total_cost=result.total_cost,
                total_cost_delta=_decimal_text(candidate_cost - baseline_cost),
                baseline_evaluation_sha256=result.identity_sha256,
            )
        )

    return ExternalValidityReport(
        protocol_sha256=protocol.identity_sha256,
        evidence_scope_sha256=protocol.evidence_scope.identity_sha256,
        candidate_evaluation_sha256=candidate.identity_sha256,
        comparisons=tuple(comparisons),
    )
