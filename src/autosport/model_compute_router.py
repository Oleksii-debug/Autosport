"""Deterministic, policy-bounded model/compute routing with durable evidence."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .integrity import atomic_write_json
from .sport_domain_fitness import (
    CausalView,
    RouteRecommendation,
    RouteStatus,
    SportDomainFitnessObservation,
    recommend_route,
)
from .voc_evaluation import (
    PairedVOCEvaluation,
    VOCEvaluationError,
    VOCEvaluationProvenance,
    VOCEvaluationStore,
)

_SCHEMA = "autosport.model_compute_router"
_VERSION = 6
_EXECUTION_AUTHORITY_SCHEMA = (
    "autosport.model_compute_router.execution_authority"
)
_EXECUTION_AUTHORITY_VERSION = 2
_VOC_SHADOW_AUTHORITY_SCHEMA = (
    "autosport.model_compute_router.voc_shadow_execution_authority"
)
_VOC_SHADOW_AUTHORITY_VERSION = 1
_VOC_SHADOW_AUTHORITY_FIELDS = {
    "schema",
    "version",
    "authority_sequence",
    "previous_authority_sha256",
    "authority_recorded_at",
    "request_id",
    "role",
    "route_record_sha256",
    "candidate_identity",
    "output_sha256",
    "action",
    "abstained",
    "completed_at",
    "available_at",
    "actual_cost",
    "actual_latency_seconds",
    "evidence_sha256",
    "authority_sha256",
}
_ZERO = Decimal("0")


class ModelComputeRouterError(ValueError):
    """Raised when compute-routing state is invalid or tampered."""


class ComputeTier(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    LOCAL = "LOCAL"
    CLOUD = "CLOUD"
    WAIT = "WAIT"


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    RESTRICTED = "RESTRICTED"


class VOCEvidenceProvenance(StrEnum):
    MEASURED_SHADOW = "MEASURED_SHADOW"
    SIMULATED = "SIMULATED"


class ExecutionDisposition(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED_LATE = "REJECTED_LATE"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_IDENTITY = "REJECTED_IDENTITY"
    REJECTED_CAUSAL = "REJECTED_CAUSAL"
    REJECTED_COST = "REJECTED_COST"


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ModelComputeRouterError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ModelComputeRouterError(f"{name} must be valid UTF-8") from exc
    return value


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelComputeRouterError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelComputeRouterError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _authority_now() -> str:
    """Production-owned wall-clock stamp; tests may patch this private seam."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ModelComputeRouterError(f"{name} must be a finite Decimal")
    return value


def _nonnegative(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result < _ZERO:
        raise ModelComputeRouterError(f"{name} must be non-negative")
    return result


def _positive(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result <= _ZERO:
        raise ModelComputeRouterError(f"{name} must be positive")
    return result


def _sha256(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ModelComputeRouterError(f"{name} must be SHA-256 hex")
    return text


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _seconds(later: datetime, earlier: datetime) -> Decimal:
    delta = later - earlier
    return (
        Decimal(delta.days * 86400 + delta.seconds)
        + Decimal(delta.microseconds) / Decimal("1000000")
    )


def _verified_domain_route(
    observation: SportDomainFitnessObservation | None,
    *,
    as_of: str,
) -> RouteRecommendation | None:
    if observation is None:
        return None
    if not isinstance(observation, SportDomainFitnessObservation):
        raise TypeError(
            "domain_observation must be SportDomainFitnessObservation"
        )
    return recommend_route(
        observation,
        as_of=as_of,
        view=CausalView.AS_KNOWN_AT_DECISION,
    )


def _domain_route_payload(
    route: RouteRecommendation | None,
) -> dict[str, Any] | None:
    if route is None:
        return None
    return {
        "status": route.status.value,
        "reason": route.reason,
        "observation_id": route.observation_id,
        "domain_profile": route.domain_profile.value,
    }


@dataclass(frozen=True, slots=True)
class ComputeCandidate:
    candidate_id: str
    tier: ComputeTier
    backend_id: str
    model_id: str
    config_sha256: str
    capabilities: tuple[str, ...]
    estimated_cost: Decimal
    estimated_latency_seconds: Decimal

    def __post_init__(self) -> None:
        _text("candidate_id", self.candidate_id)
        if self.tier is ComputeTier.WAIT or not isinstance(self.tier, ComputeTier):
            raise ModelComputeRouterError(
                "candidate tier must be DETERMINISTIC, LOCAL, or CLOUD"
            )
        _text("backend_id", self.backend_id)
        _text("model_id", self.model_id)
        _sha256("config_sha256", self.config_sha256)
        if type(self.capabilities) is not tuple or not self.capabilities:
            raise ModelComputeRouterError("capabilities must be a non-empty tuple")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ModelComputeRouterError("capabilities must be unique")
        for capability in self.capabilities:
            _text("capability", capability)
        _nonnegative("estimated_cost", self.estimated_cost)
        _nonnegative("estimated_latency_seconds", self.estimated_latency_seconds)

    def payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "tier": self.tier.value,
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "config_sha256": self.config_sha256,
            "capabilities": list(self.capabilities),
            "estimated_cost": str(self.estimated_cost),
            "estimated_latency_seconds": str(self.estimated_latency_seconds),
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "ComputeCandidate":
        try:
            return cls(
                candidate_id=raw["candidate_id"],
                tier=ComputeTier(raw["tier"]),
                backend_id=raw["backend_id"],
                model_id=raw["model_id"],
                config_sha256=raw["config_sha256"],
                capabilities=tuple(raw["capabilities"]),
                estimated_cost=Decimal(raw["estimated_cost"]),
                estimated_latency_seconds=Decimal(raw["estimated_latency_seconds"]),
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            if isinstance(exc, ModelComputeRouterError):
                raise
            raise ModelComputeRouterError("invalid compute candidate payload") from exc


@dataclass(frozen=True, slots=True)
class ComputeRouteRequest:
    request_id: str
    created_at: str
    decision_deadline: str
    required_capability: str
    data_classification: DataClassification
    allow_cloud: bool
    max_cost: Decimal
    response_ttl_seconds: Decimal
    baseline_candidate_id: str
    cloud_candidate_id: str | None = None
    decision_input_sha256: str | None = None
    decision_evidence_sha256: str | None = None
    voc_regime_id: str | None = None
    voc_urgency_id: str | None = None
    voc_contradiction_state: str | None = None

    def __post_init__(self) -> None:
        _text("request_id", self.request_id)
        created = _instant("created_at", self.created_at)
        deadline = _instant("decision_deadline", self.decision_deadline)
        if deadline <= created:
            raise ModelComputeRouterError("decision_deadline must be after created_at")
        _text("required_capability", self.required_capability)
        if not isinstance(self.data_classification, DataClassification):
            raise ModelComputeRouterError(
                "data_classification must be DataClassification"
            )
        if type(self.allow_cloud) is not bool:
            raise ModelComputeRouterError("allow_cloud must be bool")
        _nonnegative("max_cost", self.max_cost)
        _positive("response_ttl_seconds", self.response_ttl_seconds)
        _text("baseline_candidate_id", self.baseline_candidate_id)
        if self.cloud_candidate_id is not None:
            _text("cloud_candidate_id", self.cloud_candidate_id)
            if self.cloud_candidate_id == self.baseline_candidate_id:
                raise ModelComputeRouterError(
                    "cloud candidate must differ from baseline"
                )
        if self.decision_input_sha256 is not None:
            _sha256("decision_input_sha256", self.decision_input_sha256)
        if self.decision_evidence_sha256 is not None:
            _sha256("decision_evidence_sha256", self.decision_evidence_sha256)
        for name in (
            "voc_regime_id",
            "voc_urgency_id",
            "voc_contradiction_state",
        ):
            value = getattr(self, name)
            if value is not None:
                _text(name, value)

    def payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "created_at": _time("created_at", self.created_at),
            "decision_deadline": _time(
                "decision_deadline", self.decision_deadline
            ),
            "required_capability": self.required_capability,
            "data_classification": self.data_classification.value,
            "allow_cloud": self.allow_cloud,
            "max_cost": str(self.max_cost),
            "response_ttl_seconds": str(self.response_ttl_seconds),
            "baseline_candidate_id": self.baseline_candidate_id,
            "cloud_candidate_id": self.cloud_candidate_id,
            "decision_input_sha256": self.decision_input_sha256,
            "decision_evidence_sha256": self.decision_evidence_sha256,
            "voc_regime_id": self.voc_regime_id,
            "voc_urgency_id": self.voc_urgency_id,
            "voc_contradiction_state": self.voc_contradiction_state,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "ComputeRouteRequest":
        try:
            return cls(
                request_id=raw["request_id"],
                created_at=raw["created_at"],
                decision_deadline=raw["decision_deadline"],
                required_capability=raw["required_capability"],
                data_classification=DataClassification(
                    raw["data_classification"]
                ),
                allow_cloud=raw["allow_cloud"],
                max_cost=Decimal(raw["max_cost"]),
                response_ttl_seconds=Decimal(raw["response_ttl_seconds"]),
                baseline_candidate_id=raw["baseline_candidate_id"],
                cloud_candidate_id=raw.get("cloud_candidate_id"),
                decision_input_sha256=raw.get("decision_input_sha256"),
                decision_evidence_sha256=raw.get("decision_evidence_sha256"),
                voc_regime_id=raw.get("voc_regime_id"),
                voc_urgency_id=raw.get("voc_urgency_id"),
                voc_contradiction_state=raw.get("voc_contradiction_state"),
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            if isinstance(exc, ModelComputeRouterError):
                raise
            raise ModelComputeRouterError("invalid route request payload") from exc


@dataclass(frozen=True, slots=True)
class ComputeRoutingPolicy:
    policy_id: str
    policy_version: int
    cloud_enabled: bool = False
    max_cloud_cost: Decimal = Decimal("0")
    voc_max_age_seconds: Decimal = Decimal("0")
    voc_min_effective_sample_size: int = 1

    def __post_init__(self) -> None:
        _text("policy_id", self.policy_id)
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise ModelComputeRouterError(
                "policy_version must be a positive integer"
            )
        if type(self.cloud_enabled) is not bool:
            raise ModelComputeRouterError("cloud_enabled must be bool")
        _nonnegative("max_cloud_cost", self.max_cloud_cost)
        _nonnegative("voc_max_age_seconds", self.voc_max_age_seconds)
        if (
            isinstance(self.voc_min_effective_sample_size, bool)
            or not isinstance(self.voc_min_effective_sample_size, int)
            or self.voc_min_effective_sample_size < 1
        ):
            raise ModelComputeRouterError(
                "voc_min_effective_sample_size must be a positive integer"
            )
        if self.cloud_enabled:
            _positive("voc_max_age_seconds", self.voc_max_age_seconds)

    def payload(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "cloud_enabled": self.cloud_enabled,
            "max_cloud_cost": str(self.max_cloud_cost),
            "voc_max_age_seconds": str(self.voc_max_age_seconds),
            "voc_min_effective_sample_size": self.voc_min_effective_sample_size,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "ComputeRoutingPolicy":
        try:
            return cls(
                policy_id=raw["policy_id"],
                policy_version=raw["policy_version"],
                cloud_enabled=raw["cloud_enabled"],
                max_cloud_cost=Decimal(raw["max_cloud_cost"]),
                voc_max_age_seconds=Decimal(raw["voc_max_age_seconds"]),
                voc_min_effective_sample_size=raw.get(
                    "voc_min_effective_sample_size", 1
                ),
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            if isinstance(exc, ModelComputeRouterError):
                raise
            raise ModelComputeRouterError(
                "invalid routing policy payload"
            ) from exc


@dataclass(frozen=True, slots=True)
class ValueOfComputationEvidence:
    evidence_id: str
    baseline_candidate_id: str
    challenger_candidate_id: str
    baseline_backend_id: str
    baseline_model_id: str
    baseline_config_sha256: str
    challenger_backend_id: str
    challenger_model_id: str
    challenger_config_sha256: str
    measured_at: str
    available_at: str
    provenance: VOCEvidenceProvenance
    baseline_utility: Decimal
    challenger_utility: Decimal
    compute_cost_penalty: Decimal
    latency_opportunity_cost_penalty: Decimal
    measured_compute_cost: Decimal
    evaluation_sha256: str
    evaluation: PairedVOCEvaluation | None = None

    def __post_init__(self) -> None:
        _text("evidence_id", self.evidence_id)
        _text("baseline_candidate_id", self.baseline_candidate_id)
        _text("challenger_candidate_id", self.challenger_candidate_id)
        _text("baseline_backend_id", self.baseline_backend_id)
        _text("baseline_model_id", self.baseline_model_id)
        _sha256("baseline_config_sha256", self.baseline_config_sha256)
        _text("challenger_backend_id", self.challenger_backend_id)
        _text("challenger_model_id", self.challenger_model_id)
        _sha256("challenger_config_sha256", self.challenger_config_sha256)
        if self.baseline_candidate_id == self.challenger_candidate_id:
            raise ModelComputeRouterError(
                "VOC evidence requires paired distinct candidates"
            )
        measured = _instant("measured_at", self.measured_at)
        available = _instant("available_at", self.available_at)
        if available < measured:
            raise ModelComputeRouterError(
                "VOC available_at precedes measured_at"
            )
        if not isinstance(self.provenance, VOCEvidenceProvenance):
            raise ModelComputeRouterError(
                "provenance must be VOCEvidenceProvenance"
            )
        _decimal("baseline_utility", self.baseline_utility)
        _decimal("challenger_utility", self.challenger_utility)
        _nonnegative("compute_cost_penalty", self.compute_cost_penalty)
        _nonnegative(
            "latency_opportunity_cost_penalty",
            self.latency_opportunity_cost_penalty,
        )
        _nonnegative("measured_compute_cost", self.measured_compute_cost)
        _sha256("evaluation_sha256", self.evaluation_sha256)
        if self.evaluation is not None:
            if not isinstance(self.evaluation, PairedVOCEvaluation):
                raise ModelComputeRouterError(
                    "evaluation must be PairedVOCEvaluation"
                )
            evaluation = self.evaluation
            if evaluation.evaluation_id != self.evidence_id:
                raise ModelComputeRouterError(
                    "VOC evaluation identity does not match evidence_id"
                )
            if evaluation.evaluation_sha256 != self.evaluation_sha256:
                raise ModelComputeRouterError(
                    "VOC evaluation digest does not match durable paired evaluation"
                )
            expected_identity = (
                self.baseline_candidate_id,
                self.baseline_backend_id,
                self.baseline_model_id,
                self.baseline_config_sha256,
                self.challenger_candidate_id,
                self.challenger_backend_id,
                self.challenger_model_id,
                self.challenger_config_sha256,
            )
            actual_identity = (
                evaluation.baseline_candidate_id,
                evaluation.baseline_backend_id,
                evaluation.baseline_model_id,
                evaluation.baseline_config_sha256,
                evaluation.challenger_candidate_id,
                evaluation.challenger_backend_id,
                evaluation.challenger_model_id,
                evaluation.challenger_config_sha256,
            )
            if actual_identity != expected_identity:
                raise ModelComputeRouterError(
                    "VOC evaluation compute identity does not match evidence"
                )
            expected_values = (
                self.baseline_utility,
                self.challenger_utility,
                self.compute_cost_penalty,
                self.latency_opportunity_cost_penalty,
                self.measured_compute_cost,
            )
            actual_values = (
                evaluation.baseline_utility,
                evaluation.challenger_utility,
                evaluation.compute_cost_penalty,
                evaluation.latency_opportunity_cost_penalty,
                evaluation.measured_compute_cost,
            )
            if actual_values != expected_values:
                raise ModelComputeRouterError(
                    "VOC evaluation utility/cost values do not match evidence"
                )
            if _instant("measured_at", self.measured_at) != _instant(
                "evaluated_at", evaluation.evaluated_at
            ):
                raise ModelComputeRouterError(
                    "VOC measured_at does not match paired evaluation time"
                )
            if (
                self.provenance is VOCEvidenceProvenance.MEASURED_SHADOW
                and evaluation.provenance
                is not VOCEvaluationProvenance.MEASURED_SHADOW
            ) or (
                self.provenance is VOCEvidenceProvenance.SIMULATED
                and evaluation.provenance
                is not VOCEvaluationProvenance.SIMULATED
            ):
                raise ModelComputeRouterError(
                    "VOC evaluation provenance does not match evidence"
                )

    @property
    def net_value(self) -> Decimal:
        return (
            self.challenger_utility
            - self.baseline_utility
            - self.compute_cost_penalty
            - self.latency_opportunity_cost_penalty
        )

    def matches_candidates(
        self,
        baseline: ComputeCandidate,
        challenger: ComputeCandidate,
    ) -> bool:
        return (
            self.baseline_candidate_id == baseline.candidate_id
            and self.baseline_backend_id == baseline.backend_id
            and self.baseline_model_id == baseline.model_id
            and self.baseline_config_sha256 == baseline.config_sha256
            and self.challenger_candidate_id == challenger.candidate_id
            and self.challenger_backend_id == challenger.backend_id
            and self.challenger_model_id == challenger.model_id
            and self.challenger_config_sha256 == challenger.config_sha256
        )

    def payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "baseline_candidate_id": self.baseline_candidate_id,
            "challenger_candidate_id": self.challenger_candidate_id,
            "baseline_backend_id": self.baseline_backend_id,
            "baseline_model_id": self.baseline_model_id,
            "baseline_config_sha256": self.baseline_config_sha256,
            "challenger_backend_id": self.challenger_backend_id,
            "challenger_model_id": self.challenger_model_id,
            "challenger_config_sha256": self.challenger_config_sha256,
            "measured_at": _time("measured_at", self.measured_at),
            "available_at": _time("available_at", self.available_at),
            "provenance": self.provenance.value,
            "baseline_utility": str(self.baseline_utility),
            "challenger_utility": str(self.challenger_utility),
            "compute_cost_penalty": str(self.compute_cost_penalty),
            "latency_opportunity_cost_penalty": str(
                self.latency_opportunity_cost_penalty
            ),
            "measured_compute_cost": str(self.measured_compute_cost),
            "evaluation_sha256": self.evaluation_sha256,
            "evaluation": (
                None if self.evaluation is None else self.evaluation.payload()
            ),
        }

    @classmethod
    def from_payload(
        cls, raw: Mapping[str, Any]
    ) -> "ValueOfComputationEvidence":
        try:
            return cls(
                evidence_id=raw["evidence_id"],
                baseline_candidate_id=raw["baseline_candidate_id"],
                challenger_candidate_id=raw["challenger_candidate_id"],
                baseline_backend_id=raw["baseline_backend_id"],
                baseline_model_id=raw["baseline_model_id"],
                baseline_config_sha256=raw["baseline_config_sha256"],
                challenger_backend_id=raw["challenger_backend_id"],
                challenger_model_id=raw["challenger_model_id"],
                challenger_config_sha256=raw["challenger_config_sha256"],
                measured_at=raw["measured_at"],
                available_at=raw["available_at"],
                provenance=VOCEvidenceProvenance(raw["provenance"]),
                baseline_utility=Decimal(raw["baseline_utility"]),
                challenger_utility=Decimal(raw["challenger_utility"]),
                compute_cost_penalty=Decimal(raw["compute_cost_penalty"]),
                latency_opportunity_cost_penalty=Decimal(
                    raw["latency_opportunity_cost_penalty"]
                ),
                measured_compute_cost=Decimal(raw["measured_compute_cost"]),
                evaluation_sha256=raw["evaluation_sha256"],
                evaluation=(
                    None
                    if raw.get("evaluation") is None
                    else PairedVOCEvaluation.from_payload(raw["evaluation"])
                ),
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            if isinstance(exc, ModelComputeRouterError):
                raise
            raise ModelComputeRouterError(
                "invalid VOC evidence payload"
            ) from exc


@dataclass(frozen=True, slots=True)
class ComputeRouteDecision:
    decision_id: str
    request_id: str
    decided_at: str
    policy_id: str
    policy_version: int
    tier: ComputeTier
    candidate_id: str | None
    backend_id: str | None
    model_id: str | None
    config_sha256: str | None
    estimated_cost: Decimal
    estimated_latency_seconds: Decimal
    reason: str
    voc_evidence_id: str | None
    domain_observation_id: str | None
    decision_sha256: str

    def __post_init__(self) -> None:
        _text("decision_id", self.decision_id)
        _text("request_id", self.request_id)
        _time("decided_at", self.decided_at)
        _text("policy_id", self.policy_id)
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise ModelComputeRouterError("policy_version must be positive")
        if not isinstance(self.tier, ComputeTier):
            raise ModelComputeRouterError("tier must be ComputeTier")
        _nonnegative("estimated_cost", self.estimated_cost)
        _nonnegative(
            "estimated_latency_seconds", self.estimated_latency_seconds
        )
        _text("reason", self.reason)
        if self.tier is ComputeTier.WAIT:
            if any(
                value is not None
                for value in (
                    self.candidate_id,
                    self.backend_id,
                    self.model_id,
                    self.config_sha256,
                )
            ):
                raise ModelComputeRouterError(
                    "WAIT decision cannot bind a compute identity"
                )
        else:
            _text("candidate_id", self.candidate_id)
            _text("backend_id", self.backend_id)
            _text("model_id", self.model_id)
            _sha256("config_sha256", self.config_sha256)
        if self.voc_evidence_id is not None:
            _text("voc_evidence_id", self.voc_evidence_id)
        if self.domain_observation_id is not None:
            _text("domain_observation_id", self.domain_observation_id)
        _sha256("decision_sha256", self.decision_sha256)
        if self.decision_sha256 != _canonical_digest(
            self._unsigned_payload()
        ):
            raise ModelComputeRouterError(
                "decision SHA-256 does not match payload"
            )

    def _unsigned_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "decided_at": _time("decided_at", self.decided_at),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "tier": self.tier.value,
            "candidate_id": self.candidate_id,
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "config_sha256": self.config_sha256,
            "estimated_cost": str(self.estimated_cost),
            "estimated_latency_seconds": str(
                self.estimated_latency_seconds
            ),
            "reason": self.reason,
            "voc_evidence_id": self.voc_evidence_id,
            "domain_observation_id": self.domain_observation_id,
        }

    def payload(self) -> dict[str, Any]:
        return {
            **self._unsigned_payload(),
            "decision_sha256": self.decision_sha256,
        }

    @classmethod
    def build(
        cls,
        *,
        decision_id: str,
        request_id: str,
        decided_at: str,
        policy: ComputeRoutingPolicy,
        tier: ComputeTier,
        candidate: ComputeCandidate | None,
        reason: str,
        voc_evidence_id: str | None,
        domain_observation_id: str | None,
    ) -> "ComputeRouteDecision":
        unsigned = {
            "decision_id": decision_id,
            "request_id": request_id,
            "decided_at": _time("decided_at", decided_at),
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "tier": tier.value,
            "candidate_id": (
                None if candidate is None else candidate.candidate_id
            ),
            "backend_id": (
                None if candidate is None else candidate.backend_id
            ),
            "model_id": (
                None if candidate is None else candidate.model_id
            ),
            "config_sha256": (
                None if candidate is None else candidate.config_sha256
            ),
            "estimated_cost": str(
                _ZERO if candidate is None else candidate.estimated_cost
            ),
            "estimated_latency_seconds": str(
                _ZERO
                if candidate is None
                else candidate.estimated_latency_seconds
            ),
            "reason": reason,
            "voc_evidence_id": voc_evidence_id,
            "domain_observation_id": domain_observation_id,
        }
        return cls(
            decision_id=decision_id,
            request_id=request_id,
            decided_at=unsigned["decided_at"],
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            tier=tier,
            candidate_id=unsigned["candidate_id"],
            backend_id=unsigned["backend_id"],
            model_id=unsigned["model_id"],
            config_sha256=unsigned["config_sha256"],
            estimated_cost=Decimal(unsigned["estimated_cost"]),
            estimated_latency_seconds=Decimal(
                unsigned["estimated_latency_seconds"]
            ),
            reason=reason,
            voc_evidence_id=voc_evidence_id,
            domain_observation_id=domain_observation_id,
            decision_sha256=_canonical_digest(unsigned),
        )

    @classmethod
    def from_payload(
        cls, raw: Mapping[str, Any]
    ) -> "ComputeRouteDecision":
        try:
            return cls(
                decision_id=raw["decision_id"],
                request_id=raw["request_id"],
                decided_at=raw["decided_at"],
                policy_id=raw["policy_id"],
                policy_version=raw["policy_version"],
                tier=ComputeTier(raw["tier"]),
                candidate_id=raw.get("candidate_id"),
                backend_id=raw.get("backend_id"),
                model_id=raw.get("model_id"),
                config_sha256=raw.get("config_sha256"),
                estimated_cost=Decimal(raw["estimated_cost"]),
                estimated_latency_seconds=Decimal(
                    raw["estimated_latency_seconds"]
                ),
                reason=raw["reason"],
                voc_evidence_id=raw.get("voc_evidence_id"),
                domain_observation_id=raw.get(
                    "domain_observation_id"
                ),
                decision_sha256=raw["decision_sha256"],
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            if isinstance(exc, ModelComputeRouterError):
                raise
            raise ModelComputeRouterError(
                "invalid route decision payload"
            ) from exc


@dataclass(frozen=True, slots=True)
class ComputeExecutionEvidence:
    execution_id: str
    decision_id: str
    execution_sequence: int
    prior_incurred_cost: Decimal
    completed_at: str
    available_at: str
    observed_at: str
    backend_id: str
    model_id: str
    config_sha256: str
    actual_cost: Decimal
    actual_latency_seconds: Decimal
    disposition: ExecutionDisposition
    reason: str
    evidence_sha256: str
    execution_record_sha256: str

    def __post_init__(self) -> None:
        _text("execution_id", self.execution_id)
        _text("decision_id", self.decision_id)
        if (
            type(self.execution_sequence) is not int
            or self.execution_sequence < 1
        ):
            raise ModelComputeRouterError(
                "execution_sequence must be positive"
            )
        _nonnegative(
            "prior_incurred_cost", self.prior_incurred_cost
        )
        completed = _instant("completed_at", self.completed_at)
        available = _instant("available_at", self.available_at)
        observed = _instant("observed_at", self.observed_at)
        if available < completed:
            raise ModelComputeRouterError(
                "available_at precedes completed_at"
            )
        if observed < available:
            raise ModelComputeRouterError(
                "observed_at precedes available_at"
            )
        _text("backend_id", self.backend_id)
        _text("model_id", self.model_id)
        _sha256("config_sha256", self.config_sha256)
        _nonnegative("actual_cost", self.actual_cost)
        _nonnegative(
            "actual_latency_seconds", self.actual_latency_seconds
        )
        if not isinstance(self.disposition, ExecutionDisposition):
            raise ModelComputeRouterError(
                "disposition must be ExecutionDisposition"
            )
        _text("reason", self.reason)
        _sha256("evidence_sha256", self.evidence_sha256)
        _sha256(
            "execution_record_sha256",
            self.execution_record_sha256,
        )

    def _unsigned_payload(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "decision_id": self.decision_id,
            "execution_sequence": self.execution_sequence,
            "prior_incurred_cost": str(self.prior_incurred_cost),
            "completed_at": _time("completed_at", self.completed_at),
            "available_at": _time("available_at", self.available_at),
            "observed_at": _time("observed_at", self.observed_at),
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "config_sha256": self.config_sha256,
            "actual_cost": str(self.actual_cost),
            "actual_latency_seconds": str(
                self.actual_latency_seconds
            ),
            "disposition": self.disposition.value,
            "reason": self.reason,
            "evidence_sha256": self.evidence_sha256,
        }

    def payload(self) -> dict[str, Any]:
        return {
            **self._unsigned_payload(),
            "execution_record_sha256": self.execution_record_sha256,
        }

    def verify_record_sha256(self) -> None:
        if self.execution_record_sha256 != _canonical_digest(
            self._unsigned_payload()
        ):
            raise ModelComputeRouterError(
                "execution record SHA-256 does not match payload"
            )

    @classmethod
    def build(
        cls,
        *,
        execution_id: str,
        decision_id: str,
        execution_sequence: int,
        prior_incurred_cost: Decimal,
        completed_at: str,
        available_at: str,
        observed_at: str,
        backend_id: str,
        model_id: str,
        config_sha256: str,
        actual_cost: Decimal,
        actual_latency_seconds: Decimal,
        disposition: ExecutionDisposition,
        reason: str,
        evidence_sha256: str,
    ) -> "ComputeExecutionEvidence":
        unsigned = {
            "execution_id": execution_id,
            "decision_id": decision_id,
            "execution_sequence": execution_sequence,
            "prior_incurred_cost": str(prior_incurred_cost),
            "completed_at": _time("completed_at", completed_at),
            "available_at": _time("available_at", available_at),
            "observed_at": _time("observed_at", observed_at),
            "backend_id": backend_id,
            "model_id": model_id,
            "config_sha256": config_sha256,
            "actual_cost": str(actual_cost),
            "actual_latency_seconds": str(actual_latency_seconds),
            "disposition": disposition.value,
            "reason": reason,
            "evidence_sha256": evidence_sha256,
        }
        return cls(
            execution_id=execution_id,
            decision_id=decision_id,
            execution_sequence=execution_sequence,
            prior_incurred_cost=prior_incurred_cost,
            completed_at=unsigned["completed_at"],
            available_at=unsigned["available_at"],
            observed_at=unsigned["observed_at"],
            backend_id=backend_id,
            model_id=model_id,
            config_sha256=config_sha256,
            actual_cost=actual_cost,
            actual_latency_seconds=actual_latency_seconds,
            disposition=disposition,
            reason=reason,
            evidence_sha256=evidence_sha256,
            execution_record_sha256=_canonical_digest(unsigned),
        )

    @classmethod
    def from_payload(
        cls, raw: Mapping[str, Any]
    ) -> "ComputeExecutionEvidence":
        try:
            return cls(
                execution_id=raw["execution_id"],
                decision_id=raw["decision_id"],
                execution_sequence=raw["execution_sequence"],
                prior_incurred_cost=Decimal(
                    raw["prior_incurred_cost"]
                ),
                completed_at=raw["completed_at"],
                available_at=raw["available_at"],
                observed_at=raw["observed_at"],
                backend_id=raw["backend_id"],
                model_id=raw["model_id"],
                config_sha256=raw["config_sha256"],
                actual_cost=Decimal(raw["actual_cost"]),
                actual_latency_seconds=Decimal(
                    raw["actual_latency_seconds"]
                ),
                disposition=ExecutionDisposition(raw["disposition"]),
                reason=raw["reason"],
                evidence_sha256=raw["evidence_sha256"],
                execution_record_sha256=raw[
                    "execution_record_sha256"
                ],
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            if isinstance(exc, ModelComputeRouterError):
                raise
            raise ModelComputeRouterError(
                "invalid execution evidence payload"
            ) from exc


def _execution_head_payload(
    decision_id: str,
    history: Sequence[ComputeExecutionEvidence],
) -> dict[str, Any]:
    ordered = sorted(
        history,
        key=lambda evidence: evidence.execution_sequence,
    )
    record_sha256s = [
        evidence.execution_record_sha256
        for evidence in ordered
    ]
    cumulative_incurred_cost = sum(
        (evidence.actual_cost for evidence in ordered),
        _ZERO,
    )
    unsigned = {
        "decision_id": _text("decision_id", decision_id),
        "terminal_sequence": len(ordered),
        "cumulative_incurred_cost": str(
            cumulative_incurred_cost
        ),
        "terminal_execution_record_sha256": (
            None if not ordered else record_sha256s[-1]
        ),
        "history_sha256": _canonical_digest(
            {
                "decision_id": decision_id,
                "execution_record_sha256s": record_sha256s,
            }
        ),
    }
    return {
        **unsigned,
        "head_sha256": _canonical_digest(unsigned),
    }


def _validate_execution_head_payload(
    raw: object,
) -> dict[str, Any]:
    if type(raw) is not dict:
        raise ModelComputeRouterError(
            "execution history head must be an object"
        )
    expected_keys = {
        "decision_id",
        "terminal_sequence",
        "cumulative_incurred_cost",
        "terminal_execution_record_sha256",
        "history_sha256",
        "head_sha256",
    }
    if set(raw) != expected_keys:
        raise ModelComputeRouterError(
            "execution history head fields mismatch"
        )
    decision_id = _text("decision_id", raw["decision_id"])
    terminal_sequence = raw["terminal_sequence"]
    if type(terminal_sequence) is not int or terminal_sequence < 0:
        raise ModelComputeRouterError(
            "terminal_sequence must be a non-negative integer"
        )
    try:
        cumulative = _nonnegative(
            "cumulative_incurred_cost",
            Decimal(raw["cumulative_incurred_cost"]),
        )
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise ModelComputeRouterError(
            "cumulative_incurred_cost must be a non-negative Decimal"
        ) from exc
    terminal_record = raw["terminal_execution_record_sha256"]
    if terminal_record is not None:
        _sha256(
            "terminal_execution_record_sha256",
            terminal_record,
        )
    if (terminal_sequence == 0) != (terminal_record is None):
        raise ModelComputeRouterError(
            "execution history head terminal record is inconsistent"
        )
    history_sha256 = _sha256(
        "history_sha256", raw["history_sha256"]
    )
    unsigned = {
        "decision_id": decision_id,
        "terminal_sequence": terminal_sequence,
        "cumulative_incurred_cost": str(cumulative),
        "terminal_execution_record_sha256": terminal_record,
        "history_sha256": history_sha256,
    }
    if _sha256("head_sha256", raw["head_sha256"]) != (
        _canonical_digest(unsigned)
    ):
        raise ModelComputeRouterError(
            "execution history head SHA-256 mismatch"
        )
    return {**unsigned, "head_sha256": raw["head_sha256"]}


def _execution_authority_record(
    *,
    authority_sequence: int,
    previous_authority_sha256: str | None,
    evidence: ComputeExecutionEvidence,
    execution_head: Mapping[str, Any],
) -> dict[str, Any]:
    if type(authority_sequence) is not int or authority_sequence <= 0:
        raise ModelComputeRouterError(
            "authority_sequence must be a positive integer"
        )
    if previous_authority_sha256 is not None:
        _sha256(
            "previous_authority_sha256",
            previous_authority_sha256,
        )
    head = _validate_execution_head_payload(dict(execution_head))
    if head["decision_id"] != evidence.decision_id:
        raise ModelComputeRouterError(
            "execution authority head decision does not match execution"
        )
    unsigned = {
        "schema": _EXECUTION_AUTHORITY_SCHEMA,
        "version": _EXECUTION_AUTHORITY_VERSION,
        "authority_sequence": authority_sequence,
        "previous_authority_sha256": previous_authority_sha256,
        "execution_id": evidence.execution_id,
        "decision_id": evidence.decision_id,
        "execution_record_sha256": evidence.execution_record_sha256,
        "execution": evidence.payload(),
        "execution_head": head,
    }
    return {
        **unsigned,
        "authority_sha256": _canonical_digest(unsigned),
    }


def _validate_execution_authority_record(
    raw: object,
) -> dict[str, Any]:
    if type(raw) is not dict:
        raise ModelComputeRouterError(
            "execution authority record must be an object"
        )
    expected_keys = {
        "schema",
        "version",
        "authority_sequence",
        "previous_authority_sha256",
        "execution_id",
        "decision_id",
        "execution_record_sha256",
        "execution",
        "execution_head",
        "authority_sha256",
    }
    if set(raw) != expected_keys:
        raise ModelComputeRouterError(
            "execution authority record fields mismatch"
        )
    if (
        raw["schema"] != _EXECUTION_AUTHORITY_SCHEMA
        or raw["version"] != _EXECUTION_AUTHORITY_VERSION
    ):
        raise ModelComputeRouterError(
            "execution authority schema/version is invalid"
        )
    authority_sequence = raw["authority_sequence"]
    if type(authority_sequence) is not int or authority_sequence <= 0:
        raise ModelComputeRouterError(
            "authority_sequence must be a positive integer"
        )
    previous_authority_sha256 = raw["previous_authority_sha256"]
    if previous_authority_sha256 is not None:
        previous_authority_sha256 = _sha256(
            "previous_authority_sha256",
            previous_authority_sha256,
        )
    execution_id = _text("execution_id", raw["execution_id"])
    decision_id = _text("decision_id", raw["decision_id"])
    execution_record_sha256 = _sha256(
        "execution_record_sha256",
        raw["execution_record_sha256"],
    )
    execution = ComputeExecutionEvidence.from_payload(raw["execution"])
    execution.verify_record_sha256()
    if (
        execution.execution_id != execution_id
        or execution.decision_id != decision_id
        or execution.execution_record_sha256
        != execution_record_sha256
    ):
        raise ModelComputeRouterError(
            "execution authority full evidence identity mismatch"
        )
    execution_head = _validate_execution_head_payload(
        raw["execution_head"]
    )
    if execution_head["decision_id"] != decision_id:
        raise ModelComputeRouterError(
            "execution authority head decision mismatch"
        )
    unsigned = {
        "schema": _EXECUTION_AUTHORITY_SCHEMA,
        "version": _EXECUTION_AUTHORITY_VERSION,
        "authority_sequence": authority_sequence,
        "previous_authority_sha256": previous_authority_sha256,
        "execution_id": execution_id,
        "decision_id": decision_id,
        "execution_record_sha256": execution_record_sha256,
        "execution": execution.payload(),
        "execution_head": execution_head,
    }
    authority_sha256 = _sha256(
        "authority_sha256", raw["authority_sha256"]
    )
    if authority_sha256 != _canonical_digest(unsigned):
        raise ModelComputeRouterError(
            "execution authority SHA-256 mismatch"
        )
    return {**unsigned, "authority_sha256": authority_sha256}


def _classify_execution(
    *,
    request: ComputeRouteRequest,
    policy: ComputeRoutingPolicy,
    decision: ComputeRouteDecision,
    completed_at: str,
    available_at: str,
    observed_at: str,
    backend_id: str,
    model_id: str,
    config_sha256: str,
    actual_cost: Decimal,
    prior_incurred_cost: Decimal,
) -> tuple[ExecutionDisposition, str]:
    actual_cost_value = _nonnegative("actual_cost", actual_cost)
    prior_cost = _nonnegative(
        "prior_incurred_cost", prior_incurred_cost
    )
    cumulative_incurred_cost = prior_cost + actual_cost_value
    now = _instant("observed_at", observed_at)
    completed = _instant("completed_at", completed_at)
    available = _instant("available_at", available_at)
    if now < available:
        raise ModelComputeRouterError(
            "execution evidence is not yet causally available"
        )
    identity_matches = (
        backend_id == decision.backend_id
        and model_id == decision.model_id
        and config_sha256 == decision.config_sha256
    )
    if completed < _instant("decided_at", decision.decided_at):
        return (
            ExecutionDisposition.REJECTED_CAUSAL,
            "execution completed before routed decision",
        )
    if not identity_matches:
        return (
            ExecutionDisposition.REJECTED_IDENTITY,
            "execution backend/model/config identity differs from "
            "routed decision",
        )
    if available > _instant(
        "decision_deadline", request.decision_deadline
    ):
        return (
            ExecutionDisposition.REJECTED_LATE,
            "execution became available after decision deadline",
        )
    if _seconds(now, completed) > request.response_ttl_seconds:
        return (
            ExecutionDisposition.REJECTED_STALE,
            "execution response exceeded request response TTL",
        )
    if cumulative_incurred_cost > request.max_cost:
        return (
            ExecutionDisposition.REJECTED_COST,
            "cumulative actual execution cost exceeds request budget",
        )
    if (
        decision.tier is ComputeTier.CLOUD
        and cumulative_incurred_cost > policy.max_cloud_cost
    ):
        return (
            ExecutionDisposition.REJECTED_COST,
            "cumulative actual cloud execution cost exceeds "
            "policy cloud-cost limit",
        )
    return (
        ExecutionDisposition.ACCEPTED,
        "execution identity, deadline, availability, freshness and "
        "actual cost are valid",
    )


def _candidate_map(
    candidates: Sequence[ComputeCandidate],
) -> dict[str, ComputeCandidate]:
    result: dict[str, ComputeCandidate] = {}
    for candidate in candidates:
        if not isinstance(candidate, ComputeCandidate):
            raise TypeError(
                "candidates must contain ComputeCandidate values"
            )
        if candidate.candidate_id in result:
            raise ModelComputeRouterError(
                "candidate ids must be unique"
            )
        result[candidate.candidate_id] = candidate
    return result


def _candidate_feasible(
    candidate: ComputeCandidate,
    request: ComputeRouteRequest,
    *,
    now: datetime,
) -> bool:
    if request.required_capability not in candidate.capabilities:
        return False
    if candidate.estimated_cost > request.max_cost:
        return False
    remaining = _seconds(
        _instant("decision_deadline", request.decision_deadline),
        now,
    )
    return (
        remaining >= _ZERO
        and candidate.estimated_latency_seconds <= remaining
    )


def route_compute(
    request: ComputeRouteRequest,
    candidates: Sequence[ComputeCandidate],
    policy: ComputeRoutingPolicy,
    *,
    as_of: str,
    voc_evidence: ValueOfComputationEvidence | None = None,
    voc_evaluation_store: VOCEvaluationStore | None = None,
    domain_observation: SportDomainFitnessObservation | None = None,
    domain_route: RouteRecommendation | None = None,
) -> ComputeRouteDecision:
    if not isinstance(request, ComputeRouteRequest):
        raise TypeError("request must be ComputeRouteRequest")
    if not isinstance(policy, ComputeRoutingPolicy):
        raise TypeError("policy must be ComputeRoutingPolicy")
    now = _instant("as_of", as_of)
    if domain_route is not None and not isinstance(
        domain_route, RouteRecommendation
    ):
        raise TypeError("domain_route must be RouteRecommendation")
    verified_domain_route = _verified_domain_route(
        domain_observation,
        as_of=as_of,
    )
    domain_observation_id = (
        None
        if domain_observation is None
        else domain_observation.observation_id
    )
    if now < _instant("created_at", request.created_at):
        raise ModelComputeRouterError(
            "as_of precedes request creation"
        )
    if now > _instant(
        "decision_deadline", request.decision_deadline
    ):
        return ComputeRouteDecision.build(
            decision_id=f"{request.request_id}:wait",
            request_id=request.request_id,
            decided_at=as_of,
            policy=policy,
            tier=ComputeTier.WAIT,
            candidate=None,
            reason="decision deadline already expired",
            voc_evidence_id=None,
            domain_observation_id=domain_observation_id,
        )

    by_id = _candidate_map(candidates)
    baseline = by_id.get(request.baseline_candidate_id)
    if baseline is None or baseline.tier is ComputeTier.CLOUD:
        raise ModelComputeRouterError(
            "baseline candidate must exist and be deterministic/local"
        )
    if not _candidate_feasible(baseline, request, now=now):
        return ComputeRouteDecision.build(
            decision_id=f"{request.request_id}:wait",
            request_id=request.request_id,
            decided_at=as_of,
            policy=policy,
            tier=ComputeTier.WAIT,
            candidate=None,
            reason=(
                "baseline candidate is unavailable, over budget, "
                "lacks capability, or misses deadline"
            ),
            voc_evidence_id=None,
            domain_observation_id=domain_observation_id,
        )

    if request.cloud_candidate_id is None:
        return ComputeRouteDecision.build(
            decision_id=(
                f"{request.request_id}:{baseline.candidate_id}"
            ),
            request_id=request.request_id,
            decided_at=as_of,
            policy=policy,
            tier=baseline.tier,
            candidate=baseline,
            reason=(
                "bounded baseline route selected; "
                "no cloud candidate requested"
            ),
            voc_evidence_id=None,
            domain_observation_id=domain_observation_id,
        )

    cloud = by_id.get(request.cloud_candidate_id)
    if cloud is None or cloud.tier is not ComputeTier.CLOUD:
        raise ModelComputeRouterError(
            "cloud_candidate_id must identify a CLOUD candidate"
        )

    baseline_reason = None
    if not request.allow_cloud:
        baseline_reason = (
            "request did not explicitly permit cloud compute"
        )
    elif not policy.cloud_enabled:
        baseline_reason = (
            "routing policy has cloud compute disabled"
        )
    elif (
        request.data_classification
        is not DataClassification.PUBLIC
    ):
        baseline_reason = (
            "non-public data cannot be routed to cloud compute"
        )
    elif not _candidate_feasible(cloud, request, now=now):
        baseline_reason = (
            "cloud candidate is unavailable, over budget, "
            "lacks capability, or misses deadline"
        )
    elif cloud.estimated_cost > policy.max_cloud_cost:
        baseline_reason = (
            "cloud candidate exceeds policy cloud-cost limit"
        )
    elif domain_observation is None:
        baseline_reason = (
            "missing verified sport-domain observation evidence "
            "for slower research compute"
        )
    elif (
        domain_route is not None
        and domain_route != verified_domain_route
    ):
        baseline_reason = (
            "caller-supplied sport-domain recommendation does not "
            "match observation-derived evidence"
        )
    elif (
        verified_domain_route.status
        is not RouteStatus.ROUTE_SLOW_RESEARCH
    ):
        baseline_reason = (
            "verified sport-domain evidence does not authorize "
            "slower research compute"
        )
    elif voc_evidence is None:
        baseline_reason = (
            "missing paired measured value-of-computation evidence"
        )
    elif voc_evidence.evaluation is None:
        baseline_reason = (
            "missing qualified paired outcome evaluation for VOC evidence"
        )
    else:
        if not voc_evidence.matches_candidates(baseline, cloud):
            baseline_reason = (
                "VOC evidence exact compute identity does not match "
                "the selected baseline/cloud candidates"
            )
        elif (
            voc_evidence.provenance
            is not VOCEvidenceProvenance.MEASURED_SHADOW
        ):
            baseline_reason = (
                "simulated VOC evidence cannot authorize "
                "cloud escalation"
            )
        elif _instant(
            "available_at", voc_evidence.available_at
        ) > now:
            baseline_reason = (
                "VOC evidence is not causally available "
                "at the decision boundary"
            )
        elif _instant(
            "measured_at", voc_evidence.measured_at
        ) > now:
            baseline_reason = (
                "VOC measurement is from the future"
            )
        elif (
            _seconds(
                now,
                _instant(
                    "measured_at", voc_evidence.measured_at
                ),
            )
            > policy.voc_max_age_seconds
        ):
            baseline_reason = "VOC evidence is stale"
        elif voc_evaluation_store is None:
            baseline_reason = (
                "missing durable VOC evaluation authority"
            )
        elif not isinstance(voc_evaluation_store, VOCEvaluationStore):
            raise TypeError(
                "voc_evaluation_store must be VOCEvaluationStore"
            )
        else:
            try:
                resolved_evaluation = voc_evaluation_store.require(
                    voc_evidence.evidence_id,
                    evaluation_sha256=voc_evidence.evaluation_sha256,
                    as_of=as_of,
                )
                resolved_score = voc_evaluation_store.require_score(
                    voc_evidence.evidence_id,
                    evaluation_sha256=voc_evidence.evaluation_sha256,
                    as_of=as_of,
                )
            except VOCEvaluationError as exc:
                baseline_reason = (
                    "durable VOC evaluation authority rejected evidence: "
                    + str(exc)
                )
            else:
                score_available_at = _instant(
                    "canonical VOC score available_at",
                    resolved_score.available_at,
                )
                evaluation_reason = None
                if resolved_evaluation.deadline_missed:
                    evaluation_reason = (
                        "challenger compute missed the frozen decision deadline"
                    )
                elif (
                    resolved_score.effective_sample_size
                    < policy.voc_min_effective_sample_size
                ):
                    evaluation_reason = (
                        "VOC evaluation effective sample size is insufficient"
                    )
                elif resolved_score.net_value <= _ZERO:
                    evaluation_reason = "measured value of computation is non-positive"
                elif resolved_score.incremental_value_interval_low <= _ZERO:
                    evaluation_reason = (
                        "VOC uncertainty interval does not establish positive incremental value"
                    )
                elif not resolved_evaluation.action_changed:
                    evaluation_reason = (
                        "extra computation did not change action or abstention"
                    )
                elif score_available_at > now:
                    evaluation_reason = "VOC cohort score is not causally available"
                elif _seconds(now, score_available_at) > policy.voc_max_age_seconds:
                    evaluation_reason = "VOC cohort score is stale"
                elif resolved_score.measured_compute_cost > request.max_cost:
                    evaluation_reason = (
                        "measured VOC compute cost exceeds request budget"
                    )
                elif resolved_score.measured_compute_cost > policy.max_cloud_cost:
                    evaluation_reason = (
                        "measured VOC compute cost exceeds policy cloud-cost limit"
                    )
                if (
                    resolved_evaluation.payload()
                    != voc_evidence.evaluation.payload()
                ):
                    baseline_reason = (
                        "durable VOC evaluation differs from routed evidence"
                    )
                elif evaluation_reason is not None:
                    baseline_reason = evaluation_reason
                elif (
                    _instant("available_at", voc_evidence.available_at)
                    > _instant("created_at", request.created_at)
                ):
                    baseline_reason = (
                        "qualified VOC evidence was not available when "
                        "the current request began"
                    )
                elif request.decision_input_sha256 is None:
                    baseline_reason = (
                        "missing canonical current decision-input identity"
                    )
                elif request.decision_evidence_sha256 is None:
                    baseline_reason = (
                        "missing canonical current decision-context identity"
                    )
                elif (
                    request.voc_regime_id is None
                    or request.voc_urgency_id is None
                    or request.voc_contradiction_state is None
                ):
                    baseline_reason = (
                        "missing canonical VOC routing stratum"
                    )
                else:
                    try:
                        current_context = (
                            voc_evaluation_store.require_decision_context(
                                request.decision_evidence_sha256,
                                as_of=request.created_at,
                            )
                        )
                        source_context = (
                            voc_evaluation_store.require_decision_context(
                                resolved_evaluation.decision_context_sha256,
                                as_of=resolved_evaluation.decision_at,
                            )
                        )
                    except VOCEvaluationError as exc:
                        baseline_reason = (
                            "canonical current VOC decision context rejected: "
                            + str(exc)
                        )
                    else:
                        canonical_data_classification = current_context.get(
                            "data_classification"
                        )
                        canonical_policy_id = current_context.get(
                            "routing_policy_id"
                        )
                        canonical_policy_version = current_context.get(
                            "routing_policy_version"
                        )
                        canonical_policy_sha256 = current_context.get(
                            "routing_policy_sha256"
                        )
                        canonical_cloud_permission = current_context.get(
                            "cloud_permission"
                        )
                        canonical_cloud_backend_id = current_context.get(
                            "cloud_backend_id"
                        )
                        expected_current_context = {
                            "request_id": request.request_id,
                            "decision_input_sha256": request.decision_input_sha256,
                            "task_class": request.required_capability,
                            "data_classification": request.data_classification.value,
                            "sport_id": domain_observation.sport_id,
                            "league_id": domain_observation.league_id,
                            "regime_id": request.voc_regime_id,
                            "urgency_id": request.voc_urgency_id,
                            "contradiction_state": (
                                request.voc_contradiction_state
                            ),
                            "routing_policy_id": policy.policy_id,
                            "routing_policy_version": str(policy.policy_version),
                            "routing_policy_sha256": _canonical_digest(
                                policy.payload()
                            ),
                            "cloud_permission": "ALLOW",
                            "cloud_backend_id": cloud.backend_id,
                        }
                        if canonical_data_classification is None:
                            baseline_reason = (
                                "canonical current VOC decision context is legacy "
                                "and lacks data classification"
                            )
                        elif (
                            canonical_data_classification
                            != request.data_classification.value
                        ):
                            baseline_reason = (
                                "canonical current VOC data classification "
                                "does not match the current request"
                            )
                        elif (
                            canonical_policy_id is None
                            or canonical_policy_version is None
                            or canonical_policy_sha256 is None
                            or canonical_cloud_permission is None
                            or canonical_cloud_backend_id is None
                        ):
                            baseline_reason = (
                                "canonical current VOC decision context lacks "
                                "product cloud permission authority"
                            )
                        elif (
                            canonical_policy_id != policy.policy_id
                            or canonical_policy_version
                            != str(policy.policy_version)
                            or canonical_policy_sha256
                            != _canonical_digest(policy.payload())
                        ):
                            baseline_reason = (
                                "canonical current VOC routing policy "
                                "does not match the current policy"
                            )
                        elif canonical_cloud_permission != "ALLOW":
                            baseline_reason = (
                                "canonical current VOC cloud permission "
                                "does not allow cloud compute"
                            )
                        elif canonical_cloud_backend_id != cloud.backend_id:
                            baseline_reason = (
                                "canonical current VOC cloud backend "
                                "does not match the selected cloud candidate"
                            )
                        elif current_context != expected_current_context:
                            baseline_reason = (
                                "canonical current VOC decision context "
                                "does not match the current request"
                            )
                        elif (
                            resolved_evaluation.decision_context_sha256
                            == request.decision_evidence_sha256
                            or source_context["decision_input_sha256"]
                            == current_context["decision_input_sha256"]
                            or source_context["request_id"]
                            == current_context["request_id"]
                        ):
                            baseline_reason = (
                                "historical VOC evidence cannot authorize its "
                                "source decision"
                            )
                        elif (
                            (
                                request.required_capability,
                                domain_observation.sport_id,
                                domain_observation.league_id,
                                request.voc_regime_id,
                                request.voc_urgency_id,
                                request.voc_contradiction_state,
                            )
                            != (
                                resolved_evaluation.task_class,
                                resolved_evaluation.sport_id,
                                resolved_evaluation.league_id,
                                resolved_evaluation.regime_id,
                                resolved_evaluation.urgency_id,
                                resolved_evaluation.contradiction_state,
                            )
                        ):
                            baseline_reason = (
                                "qualified VOC routing stratum does not match "
                                "the current request"
                            )

    if baseline_reason is not None:
        return ComputeRouteDecision.build(
            decision_id=(
                f"{request.request_id}:{baseline.candidate_id}"
            ),
            request_id=request.request_id,
            decided_at=as_of,
            policy=policy,
            tier=baseline.tier,
            candidate=baseline,
            reason=(
                baseline_reason
                + "; fail closed to baseline"
            ),
            voc_evidence_id=(
                None
                if voc_evidence is None
                else voc_evidence.evidence_id
            ),
            domain_observation_id=domain_observation_id,
        )

    return ComputeRouteDecision.build(
        decision_id=(
            f"{request.request_id}:{cloud.candidate_id}"
        ),
        request_id=request.request_id,
        decided_at=as_of,
        policy=policy,
        tier=ComputeTier.CLOUD,
        candidate=cloud,
        reason=(
            "verified sport-domain evidence plus explicit public-data "
            "cloud permission and fresh positive paired measured VOC evidence"
        ),
        voc_evidence_id=voc_evidence.evidence_id,
        domain_observation_id=domain_observation_id,
    )


def _validated_voc_shadow_identity(value: object) -> dict[str, str]:
    if type(value) is not dict or set(value) != {
        "candidate_id",
        "backend_id",
        "model_id",
        "config_sha256",
    }:
        raise ModelComputeRouterError(
            "VOC shadow execution candidate identity is invalid"
        )
    return {
        "candidate_id": _text("candidate_id", value["candidate_id"]),
        "backend_id": _text("backend_id", value["backend_id"]),
        "model_id": _text("model_id", value["model_id"]),
        "config_sha256": _sha256(
            "config_sha256", value["config_sha256"]
        ),
    }


def _validate_voc_shadow_authority_record(
    raw: object,
) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != _VOC_SHADOW_AUTHORITY_FIELDS:
        raise ModelComputeRouterError(
            "VOC shadow execution authority schema is invalid"
        )
    if (
        raw.get("schema") != _VOC_SHADOW_AUTHORITY_SCHEMA
        or raw.get("version") != _VOC_SHADOW_AUTHORITY_VERSION
    ):
        raise ModelComputeRouterError(
            "VOC shadow execution authority version is unsupported"
        )
    sequence = raw.get("authority_sequence")
    if type(sequence) is not int or sequence < 1:
        raise ModelComputeRouterError(
            "VOC shadow execution authority sequence is invalid"
        )
    previous = raw.get("previous_authority_sha256")
    if previous is not None:
        _sha256("previous_authority_sha256", previous)
    _time("authority_recorded_at", raw.get("authority_recorded_at"))
    _text("request_id", raw.get("request_id"))
    role = _text("role", raw.get("role"))
    if role not in {"baseline", "challenger"}:
        raise ModelComputeRouterError(
            "VOC shadow execution role is invalid"
        )
    _sha256("route_record_sha256", raw.get("route_record_sha256"))
    _validated_voc_shadow_identity(raw.get("candidate_identity"))
    _sha256("output_sha256", raw.get("output_sha256"))
    _text("action", raw.get("action"))
    if type(raw.get("abstained")) is not bool:
        raise ModelComputeRouterError(
            "VOC shadow execution abstained must be bool"
        )
    _time("completed_at", raw.get("completed_at"))
    _time("available_at", raw.get("available_at"))
    try:
        actual_cost = Decimal(raw.get("actual_cost"))
        actual_latency = Decimal(raw.get("actual_latency_seconds"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ModelComputeRouterError(
            "VOC shadow execution measurements are invalid"
        ) from exc
    _nonnegative("actual_cost", actual_cost)
    _nonnegative("actual_latency_seconds", actual_latency)
    _sha256("evidence_sha256", raw.get("evidence_sha256"))
    authority_sha = _sha256(
        "authority_sha256", raw.get("authority_sha256")
    )
    unsigned = {
        key: raw[key]
        for key in _VOC_SHADOW_AUTHORITY_FIELDS
        if key != "authority_sha256"
    }
    if authority_sha != _canonical_digest(unsigned):
        raise ModelComputeRouterError(
            "VOC shadow execution authority SHA-256 mismatch"
        )
    return dict(raw)


def _voc_shadow_authority_record(
    *,
    authority_sequence: int,
    previous_authority_sha256: str | None,
    authority_recorded_at: str,
    request_id: str,
    role: str,
    route_record_sha256: str,
    candidate_identity: Mapping[str, str],
    output_sha256: str,
    action: str,
    abstained: bool,
    completed_at: str,
    available_at: str,
    actual_cost: Decimal,
    actual_latency_seconds: Decimal,
    evidence_sha256: str,
) -> dict[str, Any]:
    unsigned = {
        "schema": _VOC_SHADOW_AUTHORITY_SCHEMA,
        "version": _VOC_SHADOW_AUTHORITY_VERSION,
        "authority_sequence": authority_sequence,
        "previous_authority_sha256": previous_authority_sha256,
        "authority_recorded_at": _time(
            "authority_recorded_at", authority_recorded_at
        ),
        "request_id": _text("request_id", request_id),
        "role": _text("role", role),
        "route_record_sha256": _sha256(
            "route_record_sha256", route_record_sha256
        ),
        "candidate_identity": _validated_voc_shadow_identity(
            dict(candidate_identity)
        ),
        "output_sha256": _sha256("output_sha256", output_sha256),
        "action": _text("action", action),
        "abstained": abstained,
        "completed_at": _time("completed_at", completed_at),
        "available_at": _time("available_at", available_at),
        "actual_cost": str(_nonnegative("actual_cost", actual_cost)),
        "actual_latency_seconds": str(
            _nonnegative(
                "actual_latency_seconds", actual_latency_seconds
            )
        ),
        "evidence_sha256": _sha256(
            "evidence_sha256", evidence_sha256
        ),
    }
    if role not in {"baseline", "challenger"}:
        raise ModelComputeRouterError(
            "VOC shadow execution role is invalid"
        )
    if type(abstained) is not bool:
        raise ModelComputeRouterError(
            "VOC shadow execution abstained must be bool"
        )
    return {
        **unsigned,
        "authority_sha256": _canonical_digest(unsigned),
    }


_VOC_PRECOMPUTE_CONTROL_FIELDS = {
    "admission_id",
    "research_protocol_id",
    "cohort_id",
    "baseline_candidate_id",
    "challenger_candidate_id",
    "sport_id",
    "league_id",
}
_VOC_PRECOMPUTE_FIELDS = {
    "schema_version",
    "admission_id",
    "request_id",
    "decision_id",
    "decision_input_sha256",
    "decision_context_sha256",
    "decision_deadline",
    "admitted_at",
    "authority_recorded_at",
    "research_protocol_id",
    "cohort_id",
    "task_class",
    "scope",
    "baseline_compute_identity",
    "challenger_compute_identity",
}


def _voc_compute_identity(candidate: ComputeCandidate) -> dict[str, str]:
    return {
        "candidate_id": candidate.candidate_id,
        "backend_id": candidate.backend_id,
        "model_id": candidate.model_id,
        "config_sha256": candidate.config_sha256,
    }


def _build_voc_precompute_admission(
    *,
    request: ComputeRouteRequest,
    candidates: Sequence[ComputeCandidate],
    decision: ComputeRouteDecision,
    domain_observation: SportDomainFitnessObservation | None,
    control: Mapping[str, Any],
    authority_recorded_at: str | None = None,
) -> dict[str, Any]:
    """Derive one immutable paired-shadow enrollment from canonical route inputs."""

    if type(control) is not dict or set(control) != _VOC_PRECOMPUTE_CONTROL_FIELDS:
        raise ModelComputeRouterError(
            "voc_precompute_admission control fields are invalid"
        )
    if request.decision_input_sha256 is None:
        raise ModelComputeRouterError(
            "VOC precompute admission requires decision_input_sha256"
        )
    if request.decision_evidence_sha256 is None:
        raise ModelComputeRouterError(
            "VOC precompute admission requires canonical decision context evidence"
        )
    sport_id = _text(
        "voc_precompute_admission sport_id", control["sport_id"]
    )
    league_id = _text(
        "voc_precompute_admission league_id", control["league_id"]
    )
    if domain_observation is not None and (
        domain_observation.sport_id != sport_id
        or domain_observation.league_id != league_id
    ):
        raise ModelComputeRouterError(
            "VOC precompute admission scope does not match canonical domain observation"
        )
    for name in (
        "voc_regime_id",
        "voc_urgency_id",
        "voc_contradiction_state",
    ):
        if getattr(request, name) is None:
            raise ModelComputeRouterError(
                f"VOC precompute admission requires {name}"
            )

    by_id = _candidate_map(candidates)
    baseline_id = _text(
        "voc_precompute_admission baseline_candidate_id",
        control["baseline_candidate_id"],
    )
    baseline = by_id.get(baseline_id)
    challenger_id = _text(
        "voc_precompute_admission challenger_candidate_id",
        control["challenger_candidate_id"],
    )
    challenger = by_id.get(challenger_id)
    if baseline is None or challenger is None:
        raise ModelComputeRouterError(
            "VOC precompute admission candidates must exist in canonical route inputs"
        )
    if baseline.candidate_id == challenger.candidate_id:
        raise ModelComputeRouterError(
            "VOC precompute admission candidates must be distinct"
        )
    if (
        request.required_capability not in baseline.capabilities
        or request.required_capability not in challenger.capabilities
    ):
        raise ModelComputeRouterError(
            "VOC precompute admission candidates must support the routed task"
        )

    recorded_at = _time(
        "voc_precompute authority_recorded_at",
        _authority_now()
        if authority_recorded_at is None
        else authority_recorded_at,
    )
    if _instant("voc_precompute authority_recorded_at", recorded_at) < _instant(
        "decision.decided_at", decision.decided_at
    ):
        raise ModelComputeRouterError(
            "VOC precompute authority cannot predate canonical route decision"
        )

    return {
        "schema_version": 1,
        "admission_id": _text(
            "voc_precompute_admission admission_id",
            control["admission_id"],
        ),
        "request_id": request.request_id,
        "decision_id": decision.decision_id,
        "decision_input_sha256": request.decision_input_sha256,
        "decision_context_sha256": request.decision_evidence_sha256,
        "decision_deadline": _time(
            "decision_deadline", request.decision_deadline
        ),
        "admitted_at": _time("decision.decided_at", decision.decided_at),
        "authority_recorded_at": recorded_at,
        "research_protocol_id": _text(
            "voc_precompute_admission research_protocol_id",
            control["research_protocol_id"],
        ),
        "cohort_id": _text(
            "voc_precompute_admission cohort_id",
            control["cohort_id"],
        ),
        "task_class": request.required_capability,
        "scope": {
            "sport_id": sport_id,
            "league_id": league_id,
            "regime_id": request.voc_regime_id,
            "urgency_id": request.voc_urgency_id,
            "contradiction_state": request.voc_contradiction_state,
        },
        "baseline_compute_identity": _voc_compute_identity(baseline),
        "challenger_compute_identity": _voc_compute_identity(challenger),
    }


def _validate_persisted_voc_precompute_admission(
    *,
    request: ComputeRouteRequest,
    candidates: Sequence[ComputeCandidate],
    decision: ComputeRouteDecision,
    domain_observation: SportDomainFitnessObservation | None,
    raw: object,
) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != _VOC_PRECOMPUTE_FIELDS:
        raise ModelComputeRouterError(
            "persisted VOC precompute admission schema is invalid"
        )
    if raw.get("schema_version") != 1:
        raise ModelComputeRouterError(
            "persisted VOC precompute admission schema version is unsupported"
        )
    challenger = raw.get("challenger_compute_identity")
    if type(challenger) is not dict:
        raise ModelComputeRouterError(
            "persisted VOC challenger compute identity is invalid"
        )
    expected = _build_voc_precompute_admission(
        request=request,
        candidates=candidates,
        decision=decision,
        domain_observation=domain_observation,
        control={
            "admission_id": raw.get("admission_id"),
            "research_protocol_id": raw.get("research_protocol_id"),
            "cohort_id": raw.get("cohort_id"),
            "baseline_candidate_id": (
                raw.get("baseline_compute_identity", {}).get("candidate_id")
                if isinstance(raw.get("baseline_compute_identity"), Mapping)
                else None
            ),
            "challenger_candidate_id": challenger.get("candidate_id"),
            "sport_id": (
                raw.get("scope", {}).get("sport_id")
                if isinstance(raw.get("scope"), Mapping)
                else None
            ),
            "league_id": (
                raw.get("scope", {}).get("league_id")
                if isinstance(raw.get("scope"), Mapping)
                else None
            ),
        },
        authority_recorded_at=raw.get("authority_recorded_at"),
    )
    if raw != expected:
        raise ModelComputeRouterError(
            "persisted VOC precompute admission is not derived from canonical route inputs"
        )
    return expected


class ModelComputeRouterStore:
    """Restart-safe route/cost ledger with separate execution authority."""

    def __init__(
        self,
        path: str | Path,
        *,
        voc_evaluation_store: VOCEvaluationStore | None = None,
    ) -> None:
        if (
            voc_evaluation_store is not None
            and not isinstance(voc_evaluation_store, VOCEvaluationStore)
        ):
            raise TypeError(
                "voc_evaluation_store must be VOCEvaluationStore or None"
            )
        self.path = Path(path)
        self._voc_evaluation_store = voc_evaluation_store
        self._execution_authority_path = self.path.with_name(
            f"{self.path.name}.execution-authority.jsonl"
        )
        self._voc_shadow_authority_path = self.path.with_name(
            f"{self.path.name}.voc-shadow-authority.jsonl"
        )
        self._routes: dict[str, dict[str, Any]] = {}
        self._executions: dict[
            str, ComputeExecutionEvidence
        ] = {}
        self._execution_heads: dict[str, dict[str, Any]] = {}
        self._execution_authority_records: list[
            dict[str, Any]
        ] = []
        self._voc_shadow_authority_records: list[
            dict[str, Any]
        ] = []
        self._live_request_ids: set[str] = set()
        self._publication_interrupted = False
        if self.path.exists():
            self._load()
        else:
            self._persist()
        self._voc_shadow_authority_records = (
            self._validate_voc_shadow_authority_records(
                self._read_voc_shadow_authority_records()
            )
        )

    def _read_voc_shadow_authority_records(
        self,
    ) -> list[dict[str, Any]]:
        if not self._voc_shadow_authority_path.exists():
            return []
        try:
            lines = self._voc_shadow_authority_path.read_text(
                encoding="utf-8"
            ).splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ModelComputeRouterError(
                "VOC shadow execution authority journal is unreadable"
            ) from exc
        records: list[dict[str, Any]] = []
        previous_sha256: str | None = None
        for expected_sequence, line in enumerate(lines, start=1):
            if not line:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority journal contains a blank record"
                )
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority journal contains invalid JSON"
                ) from exc
            record = _validate_voc_shadow_authority_record(raw)
            if record["authority_sequence"] != expected_sequence:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority sequence is not contiguous"
                )
            if record["previous_authority_sha256"] != previous_sha256:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority predecessor mismatch"
                )
            records.append(record)
            previous_sha256 = record["authority_sha256"]
        return records

    def _validate_voc_shadow_authority_records(
        self,
        records: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        validated: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for source in records:
            record = _validate_voc_shadow_authority_record(dict(source))
            request_id = record["request_id"]
            role = record["role"]
            key = (request_id, role)
            if key in seen:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority duplicates request role"
                )
            seen.add(key)
            precompute = self.get_voc_precompute_admission(request_id)
            if precompute is None:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority lacks canonical precompute admission"
                )
            if (
                record["route_record_sha256"]
                != precompute["route_record_sha256"]
            ):
                raise ModelComputeRouterError(
                    "VOC shadow execution authority route digest mismatch"
                )
            expected_identity = precompute[
                f"{role}_compute_identity"
            ]
            if record["candidate_identity"] != expected_identity:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority candidate identity mismatch"
                )
            admitted_at = _instant(
                "VOC precompute admitted_at",
                precompute["admitted_at"],
            )
            completed_at = _instant(
                "VOC shadow completed_at", record["completed_at"]
            )
            available_at = _instant(
                "VOC shadow available_at", record["available_at"]
            )
            authority_recorded_at = _instant(
                "VOC shadow authority_recorded_at",
                record["authority_recorded_at"],
            )
            precompute_recorded_at = _instant(
                "VOC precompute authority_recorded_at",
                precompute["authority_recorded_at"],
            )
            deadline = _instant(
                "VOC precompute decision_deadline",
                precompute["decision_deadline"],
            )
            if completed_at < admitted_at:
                raise ModelComputeRouterError(
                    "VOC shadow execution predates precompute admission"
                )
            if completed_at < precompute_recorded_at:
                raise ModelComputeRouterError(
                    "VOC shadow execution predates physical precompute authority"
                )
            if available_at < completed_at:
                raise ModelComputeRouterError(
                    "VOC shadow execution availability predates completion"
                )
            if available_at > deadline and completed_at <= deadline:
                raise ModelComputeRouterError(
                    "VOC shadow execution completed on time but was unavailable by deadline"
                )
            if authority_recorded_at < available_at:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority predates evidence availability"
                )
            if authority_recorded_at < precompute_recorded_at:
                raise ModelComputeRouterError(
                    "VOC shadow execution authority predates precompute authority"
                )
            expected_latency = _seconds(completed_at, admitted_at)
            if Decimal(record["actual_latency_seconds"]) != expected_latency:
                raise ModelComputeRouterError(
                    "VOC shadow execution latency is not derived from canonical timestamps"
                )
            validated.append(record)
        return validated

    def _append_voc_shadow_authority(
        self,
        record: Mapping[str, Any],
    ) -> None:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self._voc_shadow_authority_path.parent.mkdir(
            parents=True, exist_ok=True
        )
        try:
            with self._voc_shadow_authority_path.open(
                "a",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(encoded)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ModelComputeRouterError(
                "VOC shadow execution authority journal is unwritable"
            ) from exc
        self._voc_shadow_authority_records.append(dict(record))

    def _read_execution_authority_records(
        self,
    ) -> list[dict[str, Any]]:
        if not self._execution_authority_path.exists():
            return []
        try:
            lines = self._execution_authority_path.read_text(
                encoding="utf-8"
            ).splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ModelComputeRouterError(
                "execution authority journal is unreadable"
            ) from exc
        records: list[dict[str, Any]] = []
        previous_sha256: str | None = None
        for expected_sequence, line in enumerate(lines, start=1):
            if not line:
                raise ModelComputeRouterError(
                    "execution authority journal contains a blank record"
                )
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ModelComputeRouterError(
                    "execution authority journal contains invalid JSON"
                ) from exc
            record = _validate_execution_authority_record(raw)
            if record["authority_sequence"] != expected_sequence:
                raise ModelComputeRouterError(
                    "execution authority sequence is not contiguous"
                )
            if (
                record["previous_authority_sha256"]
                != previous_sha256
            ):
                raise ModelComputeRouterError(
                    "execution authority predecessor mismatch"
                )
            records.append(record)
            previous_sha256 = record["authority_sha256"]
        return records

    def _append_execution_authority(
        self,
        evidence: ComputeExecutionEvidence,
        execution_head: Mapping[str, Any],
    ) -> None:
        previous_sha256 = (
            None
            if not self._execution_authority_records
            else self._execution_authority_records[-1][
                "authority_sha256"
            ]
        )
        record = _execution_authority_record(
            authority_sequence=len(
                self._execution_authority_records
            )
            + 1,
            previous_authority_sha256=previous_sha256,
            evidence=evidence,
            execution_head=execution_head,
        )
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self._execution_authority_path.parent.mkdir(
            parents=True, exist_ok=True
        )
        try:
            with self._execution_authority_path.open(
                "a",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(encoded)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ModelComputeRouterError(
                "execution authority journal is unwritable"
            ) from exc
        self._execution_authority_records.append(record)

    def _validate_execution_authority_records(
        self,
        loaded_executions: Mapping[
            str, ComputeExecutionEvidence
        ],
        loaded_execution_heads: Mapping[
            str, Mapping[str, Any]
        ],
    ) -> list[dict[str, Any]]:
        records = self._read_execution_authority_records()
        if loaded_executions and not records:
            raise ModelComputeRouterError(
                "execution authority journal is missing"
            )

        seen_execution_ids: set[str] = set()
        records_by_decision: dict[
            str, list[dict[str, Any]]
        ] = {}
        for record in records:
            execution_id = record["execution_id"]
            if execution_id in seen_execution_ids:
                raise ModelComputeRouterError(
                    "duplicate execution authority record"
                )
            seen_execution_ids.add(execution_id)
            evidence = loaded_executions.get(execution_id)
            if evidence is None:
                raise ModelComputeRouterError(
                    "execution authority references missing execution"
                )
            if (
                evidence.decision_id != record["decision_id"]
                or evidence.execution_record_sha256
                != record["execution_record_sha256"]
            ):
                raise ModelComputeRouterError(
                    "execution authority does not match execution evidence"
                )
            if (
                record["execution_head"]["terminal_sequence"]
                != evidence.execution_sequence
            ):
                raise ModelComputeRouterError(
                    "execution authority sequence does not match evidence"
                )
            records_by_decision.setdefault(
                evidence.decision_id, []
            ).append(record)

        histories: dict[
            str, list[ComputeExecutionEvidence]
        ] = {}
        for evidence in loaded_executions.values():
            histories.setdefault(evidence.decision_id, []).append(
                evidence
            )

        for decision_id, persisted_head in loaded_execution_heads.items():
            history = sorted(
                histories.get(decision_id, []),
                key=lambda item: item.execution_sequence,
            )
            decision_records = records_by_decision.get(
                decision_id, []
            )
            if len(decision_records) != len(history):
                raise ModelComputeRouterError(
                    "execution authority history length mismatch"
                )
            for index, (evidence, record) in enumerate(
                zip(history, decision_records, strict=True),
                start=1,
            ):
                if record["execution_id"] != evidence.execution_id:
                    raise ModelComputeRouterError(
                        "execution authority order mismatch"
                    )
                expected_head = _execution_head_payload(
                    decision_id,
                    history[:index],
                )
                if record["execution_head"] != expected_head:
                    raise ModelComputeRouterError(
                        "execution authority prefix head mismatch"
                    )
            if history and (
                decision_records[-1]["execution_head"]
                != persisted_head
            ):
                raise ModelComputeRouterError(
                    "routing store terminal head does not match "
                    "separate execution authority"
                )

        return records

    def _recover_interrupted_execution_publication(
        self,
        loaded_routes: Mapping[str, Mapping[str, Any]],
        loaded_decision_authority: Mapping[
            str,
            tuple[
                ComputeRouteRequest,
                ComputeRoutingPolicy,
                ComputeRouteDecision,
            ],
        ],
        loaded_executions: Mapping[str, ComputeExecutionEvidence],
        loaded_execution_heads: Mapping[str, Mapping[str, Any]],
    ) -> tuple[
        dict[str, ComputeExecutionEvidence],
        dict[str, dict[str, Any]],
    ]:
        """Complete one journal-first execution publication after a crash.

        The authority journal is fsynced before router.json. Exactly one
        validated authority record may therefore be ahead after interruption.
        Any wider or opposite-direction gap is fail-closed.
        """
        records = self._read_execution_authority_records()
        execution_state = dict(loaded_executions)
        head_state = {
            key: dict(value)
            for key, value in loaded_execution_heads.items()
        }
        if len(records) <= len(execution_state):
            return execution_state, head_state
        if len(records) != len(execution_state) + 1:
            raise ModelComputeRouterError(
                "execution authority/state publication gap is not recoverable"
            )

        prior_records = records[:-1]
        if {
            record["execution_id"] for record in prior_records
        } != set(execution_state):
            raise ModelComputeRouterError(
                "execution authority prefix does not match routing state"
            )
        for record in prior_records:
            evidence = execution_state[record["execution_id"]]
            if (
                record["decision_id"] != evidence.decision_id
                or record["execution_record_sha256"]
                != evidence.execution_record_sha256
                or record["execution"] != evidence.payload()
            ):
                raise ModelComputeRouterError(
                    "execution authority prefix evidence mismatch"
                )

        record = records[-1]
        evidence = ComputeExecutionEvidence.from_payload(
            record["execution"]
        )
        evidence.verify_record_sha256()
        if evidence.execution_id in execution_state:
            raise ModelComputeRouterError(
                "execution authority recovery tail duplicates routing state"
            )
        authority = loaded_decision_authority.get(evidence.decision_id)
        if authority is None:
            raise ModelComputeRouterError(
                "execution authority recovery references unknown decision"
            )
        request, policy, decision = authority
        if decision.tier is ComputeTier.WAIT:
            raise ModelComputeRouterError(
                "execution authority recovery references WAIT decision"
            )
        persisted_head = head_state.get(evidence.decision_id)
        if persisted_head is None:
            raise ModelComputeRouterError(
                "execution authority recovery lacks prior execution head"
            )
        history = sorted(
            (
                item
                for item in execution_state.values()
                if item.decision_id == evidence.decision_id
            ),
            key=lambda item: item.execution_sequence,
        )
        expected_prior_head = _execution_head_payload(
            evidence.decision_id,
            history,
        )
        if persisted_head != expected_prior_head:
            raise ModelComputeRouterError(
                "execution authority recovery prior head mismatch"
            )
        prior_incurred_cost = Decimal(
            persisted_head["cumulative_incurred_cost"]
        )
        if (
            evidence.execution_sequence
            != persisted_head["terminal_sequence"] + 1
            or evidence.prior_incurred_cost != prior_incurred_cost
        ):
            raise ModelComputeRouterError(
                "execution authority recovery sequence/cost mismatch"
            )
        expected_disposition, expected_reason = _classify_execution(
            request=request,
            policy=policy,
            decision=decision,
            completed_at=evidence.completed_at,
            available_at=evidence.available_at,
            observed_at=evidence.observed_at,
            backend_id=evidence.backend_id,
            model_id=evidence.model_id,
            config_sha256=evidence.config_sha256,
            actual_cost=evidence.actual_cost,
            prior_incurred_cost=prior_incurred_cost,
        )
        if (
            evidence.disposition is not expected_disposition
            or evidence.reason != expected_reason
        ):
            raise ModelComputeRouterError(
                "execution authority recovery disposition is not reproducible"
            )
        expected_head = _execution_head_payload(
            evidence.decision_id,
            [*history, evidence],
        )
        if record["execution_head"] != expected_head:
            raise ModelComputeRouterError(
                "execution authority recovery terminal head mismatch"
            )

        execution_state[evidence.execution_id] = evidence
        head_state[evidence.decision_id] = expected_head
        try:
            self._persist(
                loaded_routes,
                execution_state,
                head_state,
            )
        except OSError as exc:
            raise ModelComputeRouterError(
                "execution authority recovery could not repair routing state"
            ) from exc
        return execution_state, head_state

    @staticmethod
    def _body(
        routes: Mapping[str, Mapping[str, Any]],
        executions: Mapping[str, ComputeExecutionEvidence],
        execution_heads: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "version": _VERSION,
            "routes": [
                routes[key] for key in sorted(routes)
            ],
            "executions": [
                executions[key].payload()
                for key in sorted(executions)
            ],
            "execution_heads": [
                execution_heads[key]
                for key in sorted(execution_heads)
            ],
        }

    def _persist(
        self,
        routes: Mapping[
            str, Mapping[str, Any]
        ] | None = None,
        executions: Mapping[
            str, ComputeExecutionEvidence
        ] | None = None,
        execution_heads: Mapping[
            str, Mapping[str, Any]
        ] | None = None,
    ) -> None:
        route_state = (
            self._routes if routes is None else routes
        )
        execution_state = (
            self._executions
            if executions is None
            else executions
        )
        execution_head_state = (
            self._execution_heads
            if execution_heads is None
            else execution_heads
        )
        body = self._body(
            route_state,
            execution_state,
            execution_head_state,
        )
        atomic_write_json(
            self.path,
            {
                **body,
                "state_sha256": _canonical_digest(body),
            },
        )

    def _load(self) -> None:
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ModelComputeRouterError(
                "routing store is unreadable"
            ) from exc
        if (
            type(raw) is not dict
            or raw.get("schema") != _SCHEMA
            or raw.get("version") != _VERSION
        ):
            raise ModelComputeRouterError(
                "routing store schema/version is invalid"
            )
        state_sha256 = raw.get("state_sha256")
        body = {
            key: raw.get(key)
            for key in (
                "schema",
                "version",
                "routes",
                "executions",
                "execution_heads",
            )
        }
        if (
            _sha256("state_sha256", state_sha256)
            != _canonical_digest(body)
        ):
            raise ModelComputeRouterError(
                "routing store state SHA-256 mismatch"
            )
        routes = raw.get("routes")
        executions = raw.get("executions")
        execution_heads = raw.get("execution_heads")
        if (
            type(routes) is not list
            or type(executions) is not list
            or type(execution_heads) is not list
        ):
            raise ModelComputeRouterError(
                "routing store collections are invalid"
            )
        loaded_routes: dict[
            str, dict[str, Any]
        ] = {}
        loaded_decision_authority: dict[
            str,
            tuple[
                ComputeRouteRequest,
                ComputeRoutingPolicy,
                ComputeRouteDecision,
            ],
        ] = {}
        for item in routes:
            if type(item) is not dict:
                raise ModelComputeRouterError(
                    "route record must be an object"
                )
            request = ComputeRouteRequest.from_payload(
                item["request"]
            )
            policy = ComputeRoutingPolicy.from_payload(
                item["policy"]
            )
            candidates = tuple(
                ComputeCandidate.from_payload(x)
                for x in item["candidates"]
            )
            decision = ComputeRouteDecision.from_payload(
                item["decision"]
            )
            voc = (
                None
                if item.get("voc_evidence") is None
                else ValueOfComputationEvidence.from_payload(
                    item["voc_evidence"]
                )
            )
            candidates_by_id = _candidate_map(candidates)
            if decision.request_id != request.request_id:
                raise ModelComputeRouterError(
                    "decision request does not match persisted request"
                )
            if (
                decision.policy_id != policy.policy_id
                or decision.policy_version != policy.policy_version
            ):
                raise ModelComputeRouterError(
                    "decision policy does not match persisted policy"
                )
            if decision.tier is not ComputeTier.WAIT:
                selected_candidate = candidates_by_id.get(
                    decision.candidate_id
                )
                expected_candidate_id = (
                    request.cloud_candidate_id
                    if decision.tier is ComputeTier.CLOUD
                    else request.baseline_candidate_id
                )
                if (
                    selected_candidate is None
                    or decision.candidate_id
                    != expected_candidate_id
                    or selected_candidate.tier is not decision.tier
                    or selected_candidate.backend_id
                    != decision.backend_id
                    or selected_candidate.model_id
                    != decision.model_id
                    or selected_candidate.config_sha256
                    != decision.config_sha256
                    or selected_candidate.estimated_cost
                    != decision.estimated_cost
                    or selected_candidate.estimated_latency_seconds
                    != decision.estimated_latency_seconds
                ):
                    raise ModelComputeRouterError(
                        "decision compute identity does not match "
                        "persisted candidate"
                    )
            if (
                decision.voc_evidence_id is not None
                and (
                    voc is None
                    or decision.voc_evidence_id != voc.evidence_id
                )
            ):
                raise ModelComputeRouterError(
                    "decision VOC evidence does not match "
                    "persisted evidence"
                )
            if (
                voc is not None
                and decision.tier is ComputeTier.CLOUD
            ):
                voc_baseline = candidates_by_id.get(
                    voc.baseline_candidate_id
                )
                voc_challenger = candidates_by_id.get(
                    voc.challenger_candidate_id
                )
                if (
                    voc_baseline is None
                    or voc_challenger is None
                    or not voc.matches_candidates(
                        voc_baseline,
                        voc_challenger,
                    )
                ):
                    raise ModelComputeRouterError(
                        "persisted CLOUD VOC exact compute identity "
                        "does not match persisted candidates"
                    )
            domain_observation = (
                None
                if item.get("domain_observation") is None
                else SportDomainFitnessObservation.from_payload(
                    item["domain_observation"]
                )
            )
            verified_domain_route = _verified_domain_route(
                domain_observation,
                as_of=decision.decided_at,
            )
            expected_domain_id = (
                None
                if domain_observation is None
                else domain_observation.observation_id
            )
            if decision.domain_observation_id != expected_domain_id:
                raise ModelComputeRouterError(
                    "decision domain observation does not match "
                    "persisted evidence"
                )
            if item.get("domain_route") != _domain_route_payload(
                verified_domain_route
            ):
                raise ModelComputeRouterError(
                    "persisted domain route is not derived from "
                    "the bound observation evidence"
                )
            persisted_voc_precompute = item.get("voc_precompute_admission")
            if persisted_voc_precompute is not None:
                persisted_voc_precompute = (
                    _validate_persisted_voc_precompute_admission(
                        request=request,
                        candidates=candidates,
                        decision=decision,
                        domain_observation=domain_observation,
                        raw=persisted_voc_precompute,
                    )
                )
            replayed_decision = route_compute(
                request,
                candidates,
                policy,
                as_of=decision.decided_at,
                voc_evidence=voc,
                voc_evaluation_store=self._voc_evaluation_store,
                domain_observation=domain_observation,
            )
            if replayed_decision.payload() != decision.payload():
                raise ModelComputeRouterError(
                    f"persisted {decision.tier.value} decision is not "
                    "authorized by persisted route inputs"
                )
            unsigned = {
                "request": request.payload(),
                "policy": policy.payload(),
                "candidates": [
                    x.payload() for x in candidates
                ],
                "decision": decision.payload(),
                "voc_evidence": (
                    None if voc is None else voc.payload()
                ),
                "domain_observation": (
                    None
                    if domain_observation is None
                    else domain_observation.payload()
                ),
                "domain_route": _domain_route_payload(
                    verified_domain_route
                ),
            }
            if persisted_voc_precompute is not None:
                unsigned["voc_precompute_admission"] = persisted_voc_precompute
            if (
                _sha256(
                    "record_sha256",
                    item.get("record_sha256"),
                )
                != _canonical_digest(unsigned)
            ):
                raise ModelComputeRouterError(
                    "route record SHA-256 mismatch"
                )
            if request.request_id in loaded_routes:
                raise ModelComputeRouterError(
                    "duplicate immutable request id "
                    "in routing store"
                )
            if decision.decision_id in loaded_decision_authority:
                raise ModelComputeRouterError(
                    "duplicate route decision id in routing store"
                )
            loaded_decision_authority[decision.decision_id] = (
                request,
                policy,
                decision,
            )
            loaded_routes[request.request_id] = item
        loaded_executions: dict[
            str, ComputeExecutionEvidence
        ] = {}
        for item in executions:
            evidence = (
                ComputeExecutionEvidence.from_payload(item)
            )
            if evidence.execution_id in loaded_executions:
                raise ModelComputeRouterError(
                    "duplicate execution id in routing store"
                )
            authority = loaded_decision_authority.get(
                evidence.decision_id
            )
            if authority is None:
                raise ModelComputeRouterError(
                    "persisted execution references unknown "
                    "route decision"
                )
            request, policy, decision = authority
            if decision.tier is ComputeTier.WAIT:
                raise ModelComputeRouterError(
                    "persisted execution references WAIT decision"
                )
            if (
                evidence.disposition
                is ExecutionDisposition.ACCEPTED
            ):
                if (
                    evidence.backend_id != decision.backend_id
                    or evidence.model_id != decision.model_id
                    or evidence.config_sha256
                    != decision.config_sha256
                ):
                    raise ModelComputeRouterError(
                        "persisted ACCEPTED execution identity "
                        "does not match route decision"
                    )
                completed = _instant(
                    "completed_at", evidence.completed_at
                )
                available = _instant(
                    "available_at", evidence.available_at
                )
                if completed < _instant(
                    "decided_at", decision.decided_at
                ):
                    raise ModelComputeRouterError(
                        "persisted ACCEPTED execution predates "
                        "route decision"
                    )
                if available > _instant(
                    "decision_deadline",
                    request.decision_deadline,
                ):
                    raise ModelComputeRouterError(
                        "persisted ACCEPTED execution became "
                        "available after decision deadline"
                    )
                if (
                    _seconds(available, completed)
                    > request.response_ttl_seconds
                ):
                    raise ModelComputeRouterError(
                        "persisted ACCEPTED execution exceeds "
                        "request response TTL"
                    )
            loaded_executions[
                evidence.execution_id
            ] = evidence

        loaded_execution_heads: dict[str, dict[str, Any]] = {}
        for item in execution_heads:
            head = _validate_execution_head_payload(item)
            decision_id = head["decision_id"]
            if decision_id in loaded_execution_heads:
                raise ModelComputeRouterError(
                    "duplicate execution history head"
                )
            if decision_id not in loaded_decision_authority:
                raise ModelComputeRouterError(
                    "execution history head references unknown decision"
                )
            loaded_execution_heads[decision_id] = head

        loaded_executions, loaded_execution_heads = (
            self._recover_interrupted_execution_publication(
                loaded_routes,
                loaded_decision_authority,
                loaded_executions,
                loaded_execution_heads,
            )
        )

        accepted_spend_by_decision: dict[str, Decimal] = {}
        for evidence in loaded_executions.values():
            if (
                evidence.disposition
                is not ExecutionDisposition.ACCEPTED
            ):
                continue
            accepted_spend_by_decision[evidence.decision_id] = (
                accepted_spend_by_decision.get(
                    evidence.decision_id, _ZERO
                )
                + evidence.actual_cost
            )
        for decision_id, accepted_spend in (
            accepted_spend_by_decision.items()
        ):
            request, policy, decision = (
                loaded_decision_authority[decision_id]
            )
            if accepted_spend > request.max_cost:
                raise ModelComputeRouterError(
                    "persisted accepted execution cost exceeds "
                    "request budget"
                )
            if (
                decision.tier is ComputeTier.CLOUD
                and accepted_spend > policy.max_cloud_cost
            ):
                raise ModelComputeRouterError(
                    "persisted accepted cloud execution cost "
                    "exceeds policy cloud-cost limit"
                )

        history_by_decision: dict[
            str, list[ComputeExecutionEvidence]
        ] = {}
        for evidence in loaded_executions.values():
            evidence.verify_record_sha256()
            history_by_decision.setdefault(
                evidence.decision_id, []
            ).append(evidence)
        for decision_id in loaded_decision_authority:
            history = history_by_decision.get(decision_id, [])
            request, policy, decision = (
                loaded_decision_authority[decision_id]
            )
            history.sort(
                key=lambda evidence: evidence.execution_sequence
            )
            prior_incurred_cost = _ZERO
            for expected_sequence, evidence in enumerate(
                history, start=1
            ):
                if evidence.execution_sequence != expected_sequence:
                    raise ModelComputeRouterError(
                        "persisted execution sequence is not "
                        "contiguous"
                    )
                if (
                    evidence.prior_incurred_cost
                    != prior_incurred_cost
                ):
                    raise ModelComputeRouterError(
                        "persisted execution prior incurred cost "
                        "does not match durable history"
                    )
                expected_disposition, expected_reason = (
                    _classify_execution(
                        request=request,
                        policy=policy,
                        decision=decision,
                        completed_at=evidence.completed_at,
                        available_at=evidence.available_at,
                        observed_at=evidence.observed_at,
                        backend_id=evidence.backend_id,
                        model_id=evidence.model_id,
                        config_sha256=evidence.config_sha256,
                        actual_cost=evidence.actual_cost,
                        prior_incurred_cost=prior_incurred_cost,
                    )
                )
                if (
                    evidence.disposition is not expected_disposition
                    or evidence.reason != expected_reason
                ):
                    raise ModelComputeRouterError(
                        "persisted execution disposition/reason "
                        "is not reproducible from durable authority"
                    )
                prior_incurred_cost += evidence.actual_cost
            persisted_head = loaded_execution_heads.get(decision_id)
            if persisted_head is None:
                raise ModelComputeRouterError(
                    "route decision lacks execution history head"
                )
            if persisted_head != _execution_head_payload(
                decision_id, history
            ):
                raise ModelComputeRouterError(
                    "persisted execution history does not match "
                    "durable terminal head"
                )
        loaded_authority_records = (
            self._validate_execution_authority_records(
                loaded_executions,
                loaded_execution_heads,
            )
        )
        self._routes = loaded_routes
        self._executions = loaded_executions
        self._execution_heads = loaded_execution_heads
        self._execution_authority_records = (
            loaded_authority_records
        )

    def route(
        self,
        request: ComputeRouteRequest,
        candidates: Sequence[ComputeCandidate],
        policy: ComputeRoutingPolicy,
        *,
        as_of: str,
        voc_evidence: ValueOfComputationEvidence | None = None,
        domain_observation: SportDomainFitnessObservation | None = None,
        domain_route: RouteRecommendation | None = None,
        voc_precompute_admission: Mapping[str, Any] | None = None,
    ) -> ComputeRouteDecision:
        if self._publication_interrupted:
            raise ModelComputeRouterError(
                "routing store has interrupted execution publication; "
                "reopen to recover"
            )
        decision = route_compute(
            request,
            candidates,
            policy,
            as_of=as_of,
            voc_evidence=voc_evidence,
            voc_evaluation_store=self._voc_evaluation_store,
            domain_observation=domain_observation,
            domain_route=domain_route,
        )
        verified_domain_route = _verified_domain_route(
            domain_observation,
            as_of=as_of,
        )
        domain_observation_payload = (
            None
            if domain_observation is None
            else domain_observation.payload()
        )
        domain_route_payload = _domain_route_payload(
            verified_domain_route
        )
        canonical_voc_precompute = (
            None
            if voc_precompute_admission is None
            else _build_voc_precompute_admission(
                request=request,
                candidates=candidates,
                decision=decision,
                domain_observation=domain_observation,
                control=voc_precompute_admission,
            )
        )
        unsigned = {
            "request": request.payload(),
            "policy": policy.payload(),
            "candidates": [
                candidate.payload()
                for candidate in candidates
            ],
            "decision": decision.payload(),
            "voc_evidence": (
                None
                if voc_evidence is None
                else voc_evidence.payload()
            ),
            "domain_observation": domain_observation_payload,
            "domain_route": domain_route_payload,
        }
        if canonical_voc_precompute is not None:
            unsigned["voc_precompute_admission"] = canonical_voc_precompute
        record = {
            **unsigned,
            "record_sha256": _canonical_digest(unsigned),
        }
        existing = self._routes.get(request.request_id)
        if existing is not None:
            if existing != record:
                raise ModelComputeRouterError(
                    "immutable request id conflicts "
                    "with stored route"
                )
            return decision
        staged = dict(self._routes)
        staged[request.request_id] = record
        staged_heads = dict(self._execution_heads)
        staged_heads[decision.decision_id] = _execution_head_payload(
            decision.decision_id,
            (),
        )
        self._persist(
            staged,
            self._executions,
            staged_heads,
        )
        self._routes = staged
        self._execution_heads = staged_heads
        self._live_request_ids.add(request.request_id)
        return decision

    def get_request(
        self, request_id: str
    ) -> ComputeRouteRequest | None:
        """Re-resolve the immutable canonical request owned by this route store."""

        _text("request_id", request_id)
        record = self._routes.get(request_id)
        return (
            None
            if record is None
            else ComputeRouteRequest.from_payload(
                record["request"]
            )
        )

    def get_decision(
        self, request_id: str
    ) -> ComputeRouteDecision | None:
        _text("request_id", request_id)
        record = self._routes.get(request_id)
        return (
            None
            if record is None
            else ComputeRouteDecision.from_payload(
                record["decision"]
            )
        )

    def get_voc_precompute_admission(
        self, request_id: str
    ) -> dict[str, Any] | None:
        """Resolve immutable router-owned paired-shadow enrollment evidence."""

        _text("request_id", request_id)
        record = self._routes.get(request_id)
        if record is None:
            return None
        raw = record.get("voc_precompute_admission")
        if raw is None:
            return None
        # _load()/route() already canonicalize this payload. Return detached
        # JSON data plus the immutable route-record digest that owns it.
        return {
            **json.loads(
                json.dumps(
                    raw,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            ),
            "route_record_sha256": record["record_sha256"],
        }

    def voc_precompute_admissions(self) -> tuple[dict[str, Any], ...]:
        """Return the complete immutable paired-shadow enrollment universe."""

        values: list[dict[str, Any]] = []
        for request_id in sorted(self._routes):
            resolved = self.get_voc_precompute_admission(request_id)
            if resolved is not None:
                values.append(resolved)
        return tuple(values)

    def get_voc_shadow_execution(
        self,
        request_id: str,
        role: str,
    ) -> dict[str, Any] | None:
        request = _text("request_id", request_id)
        role_text = _text("role", role)
        if role_text not in {"baseline", "challenger"}:
            raise ModelComputeRouterError(
                "VOC shadow execution role is invalid"
            )
        matches = [
            record
            for record in self._voc_shadow_authority_records
            if (
                record["request_id"] == request
                and record["role"] == role_text
            )
        ]
        if len(matches) > 1:
            raise ModelComputeRouterError(
                "VOC shadow execution authority is ambiguous"
            )
        if not matches:
            return None
        return json.loads(
            json.dumps(
                matches[0],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )

    def record_voc_shadow_execution(
        self,
        *,
        request_id: str,
        role: str,
        output_sha256: str,
        action: str,
        abstained: bool,
        completed_at: str,
        available_at: str,
        actual_cost: Decimal,
        evidence_sha256: str,
    ) -> dict[str, Any]:
        """Append production-owned paired-shadow execution evidence once."""

        request = _text("request_id", request_id)
        role_text = _text("role", role)
        if role_text not in {"baseline", "challenger"}:
            raise ModelComputeRouterError(
                "VOC shadow execution role is invalid"
            )
        if request not in self._live_request_ids:
            raise ModelComputeRouterError(
                "VOC shadow execution requires a live precompute route; "
                "historical/restarted requests cannot be backfilled"
            )
        precompute = self.get_voc_precompute_admission(request)
        if precompute is None:
            raise ModelComputeRouterError(
                "VOC shadow execution requires canonical precompute admission"
            )
        existing = self.get_voc_shadow_execution(request, role_text)
        if existing is not None:
            candidate = _voc_shadow_authority_record(
                authority_sequence=existing["authority_sequence"],
                previous_authority_sha256=existing[
                    "previous_authority_sha256"
                ],
                authority_recorded_at=existing[
                    "authority_recorded_at"
                ],
                request_id=request,
                role=role_text,
                route_record_sha256=precompute[
                    "route_record_sha256"
                ],
                candidate_identity=precompute[
                    f"{role_text}_compute_identity"
                ],
                output_sha256=output_sha256,
                action=action,
                abstained=abstained,
                completed_at=completed_at,
                available_at=available_at,
                actual_cost=actual_cost,
                actual_latency_seconds=Decimal(
                    existing["actual_latency_seconds"]
                ),
                evidence_sha256=evidence_sha256,
            )
            if candidate != existing:
                raise ModelComputeRouterError(
                    "immutable VOC shadow execution conflicts with stored authority"
                )
            return existing

        admitted_at = _instant(
            "VOC precompute admitted_at", precompute["admitted_at"]
        )
        precompute_recorded_at = _instant(
            "VOC precompute authority_recorded_at",
            precompute["authority_recorded_at"],
        )
        completed = _instant(
            "VOC shadow completed_at", completed_at
        )
        available = _instant(
            "VOC shadow available_at", available_at
        )
        if completed < admitted_at:
            raise ModelComputeRouterError(
                "VOC shadow execution predates precompute admission"
            )
        if completed < precompute_recorded_at:
            raise ModelComputeRouterError(
                "VOC shadow execution predates physical precompute authority"
            )
        if available < completed:
            raise ModelComputeRouterError(
                "VOC shadow execution availability predates completion"
            )
        deadline = _instant(
            "VOC precompute decision_deadline",
            precompute["decision_deadline"],
        )
        if available > deadline and completed <= deadline:
            raise ModelComputeRouterError(
                "VOC shadow execution completed on time but was unavailable by deadline"
            )
        authority_recorded_at = _time(
            "VOC shadow authority_recorded_at", _authority_now()
        )
        if _instant(
            "VOC shadow authority_recorded_at",
            authority_recorded_at,
        ) < available:
            raise ModelComputeRouterError(
                "VOC shadow execution authority predates evidence availability"
            )
        prior_sha = (
            None
            if not self._voc_shadow_authority_records
            else self._voc_shadow_authority_records[-1][
                "authority_sha256"
            ]
        )
        record = _voc_shadow_authority_record(
            authority_sequence=len(
                self._voc_shadow_authority_records
            )
            + 1,
            previous_authority_sha256=prior_sha,
            authority_recorded_at=authority_recorded_at,
            request_id=request,
            role=role_text,
            route_record_sha256=precompute[
                "route_record_sha256"
            ],
            candidate_identity=precompute[
                f"{role_text}_compute_identity"
            ],
            output_sha256=output_sha256,
            action=action,
            abstained=abstained,
            completed_at=completed_at,
            available_at=available_at,
            actual_cost=actual_cost,
            actual_latency_seconds=_seconds(
                completed, admitted_at
            ),
            evidence_sha256=evidence_sha256,
        )
        self._append_voc_shadow_authority(record)
        # Revalidate the append against the same durable route authority before
        # returning it to the caller.
        self._validate_voc_shadow_authority_records(
            self._voc_shadow_authority_records
        )
        return self.get_voc_shadow_execution(request, role_text) or record

    def record_execution(
        self,
        *,
        execution_id: str,
        request_id: str,
        completed_at: str,
        available_at: str,
        backend_id: str,
        model_id: str,
        config_sha256: str,
        actual_cost: Decimal,
        actual_latency_seconds: Decimal,
        evidence_sha256: str,
        as_of: str,
    ) -> ComputeExecutionEvidence:
        if self._publication_interrupted:
            raise ModelComputeRouterError(
                "routing store has interrupted execution publication; "
                "reopen to recover"
            )
        record = self._routes.get(
            _text("request_id", request_id)
        )
        if record is None:
            raise ModelComputeRouterError(
                "execution references unknown route request"
            )
        request = ComputeRouteRequest.from_payload(
            record["request"]
        )
        policy = ComputeRoutingPolicy.from_payload(
            record["policy"]
        )
        decision = ComputeRouteDecision.from_payload(
            record["decision"]
        )
        if decision.tier is ComputeTier.WAIT:
            raise ModelComputeRouterError(
                "WAIT decision cannot record compute execution"
            )
        actual_cost_value = _nonnegative(
            "actual_cost", actual_cost
        )
        existing = self._executions.get(execution_id)
        if (
            existing is None
            and request.request_id not in self._live_request_ids
        ):
            raise ModelComputeRouterError(
                "route request loaded from durable state is execution-frozen "
                "after restart; submit a fresh request_id"
            )
        if existing is None:
            head = self._execution_heads.get(decision.decision_id)
            if head is None:
                raise ModelComputeRouterError(
                    "route decision lacks execution history head"
                )
            execution_sequence = head["terminal_sequence"] + 1
            prior_incurred_cost = Decimal(
                head["cumulative_incurred_cost"]
            )
        else:
            execution_sequence = existing.execution_sequence
            prior_incurred_cost = existing.prior_incurred_cost
        disposition, reason = _classify_execution(
            request=request,
            policy=policy,
            decision=decision,
            completed_at=completed_at,
            available_at=available_at,
            observed_at=as_of,
            backend_id=backend_id,
            model_id=model_id,
            config_sha256=config_sha256,
            actual_cost=actual_cost_value,
            prior_incurred_cost=prior_incurred_cost,
        )
        evidence = ComputeExecutionEvidence.build(
            execution_id=execution_id,
            decision_id=decision.decision_id,
            execution_sequence=execution_sequence,
            prior_incurred_cost=prior_incurred_cost,
            completed_at=completed_at,
            available_at=available_at,
            observed_at=as_of,
            backend_id=backend_id,
            model_id=model_id,
            config_sha256=config_sha256,
            actual_cost=actual_cost_value,
            actual_latency_seconds=(
                actual_latency_seconds
            ),
            disposition=disposition,
            reason=reason,
            evidence_sha256=evidence_sha256,
        )
        if existing is not None:
            if existing != evidence:
                raise ModelComputeRouterError(
                    "immutable execution id conflicts "
                    "with stored evidence"
                )
            return evidence
        staged = dict(self._executions)
        staged[execution_id] = evidence
        decision_history = [
            item
            for item in staged.values()
            if item.decision_id == decision.decision_id
        ]
        staged_heads = dict(self._execution_heads)
        staged_heads[decision.decision_id] = _execution_head_payload(
            decision.decision_id,
            decision_history,
        )
        try:
            self._append_execution_authority(
                evidence,
                staged_heads[decision.decision_id],
            )
        except ModelComputeRouterError:
            self._publication_interrupted = True
            raise
        try:
            self._persist(
                self._routes,
                staged,
                staged_heads,
            )
        except OSError as exc:
            # The fsynced authority record is already the durable truth. Keep
            # readback in this process aligned with that incurred cost while
            # refusing every further mutation until a reopen completes repair.
            self._executions = staged
            self._execution_heads = staged_heads
            self._publication_interrupted = True
            raise ModelComputeRouterError(
                "routing-state publication failed after durable execution "
                "authority; reopen to recover"
            ) from exc
        self._executions = staged
        self._execution_heads = staged_heads
        return evidence

    def total_actual_cost(
        self, request_id: str | None = None
    ) -> Decimal:
        total = _ZERO
        decision_id = None
        if request_id is not None:
            decision = self.get_decision(request_id)
            if decision is None:
                return total
            decision_id = decision.decision_id
        for evidence in self._executions.values():
            if (
                decision_id is None
                or evidence.decision_id == decision_id
            ):
                total += evidence.actual_cost
        return total
