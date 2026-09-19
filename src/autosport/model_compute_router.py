"""Deterministic, policy-bounded model/compute routing with durable evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .integrity import atomic_write_json
from .sport_domain_fitness import RouteRecommendation, RouteStatus

_SCHEMA = "autosport.model_compute_router"
_VERSION = 1
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
        if self.cloud_enabled:
            _positive("voc_max_age_seconds", self.voc_max_age_seconds)

    def payload(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "cloud_enabled": self.cloud_enabled,
            "max_cloud_cost": str(self.max_cloud_cost),
            "voc_max_age_seconds": str(self.voc_max_age_seconds),
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
    measured_at: str
    available_at: str
    provenance: VOCEvidenceProvenance
    baseline_utility: Decimal
    challenger_utility: Decimal
    compute_cost_penalty: Decimal
    measured_compute_cost: Decimal
    evaluation_sha256: str

    def __post_init__(self) -> None:
        _text("evidence_id", self.evidence_id)
        _text("baseline_candidate_id", self.baseline_candidate_id)
        _text("challenger_candidate_id", self.challenger_candidate_id)
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
        _nonnegative("measured_compute_cost", self.measured_compute_cost)
        _sha256("evaluation_sha256", self.evaluation_sha256)

    @property
    def net_value(self) -> Decimal:
        return (
            self.challenger_utility
            - self.baseline_utility
            - self.compute_cost_penalty
        )

    def payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "baseline_candidate_id": self.baseline_candidate_id,
            "challenger_candidate_id": self.challenger_candidate_id,
            "measured_at": _time("measured_at", self.measured_at),
            "available_at": _time("available_at", self.available_at),
            "provenance": self.provenance.value,
            "baseline_utility": str(self.baseline_utility),
            "challenger_utility": str(self.challenger_utility),
            "compute_cost_penalty": str(self.compute_cost_penalty),
            "measured_compute_cost": str(self.measured_compute_cost),
            "evaluation_sha256": self.evaluation_sha256,
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
                measured_at=raw["measured_at"],
                available_at=raw["available_at"],
                provenance=VOCEvidenceProvenance(raw["provenance"]),
                baseline_utility=Decimal(raw["baseline_utility"]),
                challenger_utility=Decimal(raw["challenger_utility"]),
                compute_cost_penalty=Decimal(raw["compute_cost_penalty"]),
                measured_compute_cost=Decimal(raw["measured_compute_cost"]),
                evaluation_sha256=raw["evaluation_sha256"],
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
    completed_at: str
    available_at: str
    backend_id: str
    model_id: str
    config_sha256: str
    actual_cost: Decimal
    actual_latency_seconds: Decimal
    disposition: ExecutionDisposition
    reason: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text("execution_id", self.execution_id)
        _text("decision_id", self.decision_id)
        completed = _instant("completed_at", self.completed_at)
        available = _instant("available_at", self.available_at)
        if available < completed:
            raise ModelComputeRouterError(
                "available_at precedes completed_at"
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

    def payload(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "decision_id": self.decision_id,
            "completed_at": _time("completed_at", self.completed_at),
            "available_at": _time("available_at", self.available_at),
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

    @classmethod
    def from_payload(
        cls, raw: Mapping[str, Any]
    ) -> "ComputeExecutionEvidence":
        try:
            return cls(
                execution_id=raw["execution_id"],
                decision_id=raw["decision_id"],
                completed_at=raw["completed_at"],
                available_at=raw["available_at"],
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
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            if isinstance(exc, ModelComputeRouterError):
                raise
            raise ModelComputeRouterError(
                "invalid execution evidence payload"
            ) from exc


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
    domain_route: RouteRecommendation | None = None,
) -> ComputeRouteDecision:
    if not isinstance(request, ComputeRouteRequest):
        raise TypeError("request must be ComputeRouteRequest")
    if not isinstance(policy, ComputeRoutingPolicy):
        raise TypeError("policy must be ComputeRoutingPolicy")
    now = _instant("as_of", as_of)
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
            domain_observation_id=(
                None
                if domain_route is None
                else domain_route.observation_id
            ),
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
            domain_observation_id=(
                None
                if domain_route is None
                else domain_route.observation_id
            ),
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
            domain_observation_id=(
                None
                if domain_route is None
                else domain_route.observation_id
            ),
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
    elif (
        domain_route is not None
        and domain_route.status
        is not RouteStatus.ROUTE_SLOW_RESEARCH
    ):
        baseline_reason = (
            "sport-domain evidence does not authorize "
            "slower research compute"
        )
    elif voc_evidence is None:
        baseline_reason = (
            "missing paired measured value-of-computation evidence"
        )
    else:
        if (
            voc_evidence.baseline_candidate_id
            != baseline.candidate_id
            or voc_evidence.challenger_candidate_id
            != cloud.candidate_id
        ):
            baseline_reason = (
                "VOC evidence is not bound to the selected "
                "baseline/cloud pair"
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
        elif (
            voc_evidence.measured_compute_cost
            > policy.max_cloud_cost
        ):
            baseline_reason = (
                "measured VOC compute cost exceeds "
                "policy cloud-cost limit"
            )
        elif voc_evidence.net_value <= _ZERO:
            baseline_reason = (
                "measured value of computation is non-positive"
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
            domain_observation_id=(
                None
                if domain_route is None
                else domain_route.observation_id
            ),
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
            "explicit public-data cloud permission plus "
            "fresh positive paired measured VOC evidence"
        ),
        voc_evidence_id=voc_evidence.evidence_id,
        domain_observation_id=(
            None
            if domain_route is None
            else domain_route.observation_id
        ),
    )


class ModelComputeRouterStore:
    """Atomic restart-safe route/cost ledger with tamper detection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._routes: dict[str, dict[str, Any]] = {}
        self._executions: dict[
            str, ComputeExecutionEvidence
        ] = {}
        if self.path.exists():
            self._load()
        else:
            self._persist()

    @staticmethod
    def _body(
        routes: Mapping[str, Mapping[str, Any]],
        executions: Mapping[str, ComputeExecutionEvidence],
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
        }

    def _persist(
        self,
        routes: Mapping[
            str, Mapping[str, Any]
        ] | None = None,
        executions: Mapping[
            str, ComputeExecutionEvidence
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
        body = self._body(route_state, execution_state)
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
        if type(routes) is not list or type(executions) is not list:
            raise ModelComputeRouterError(
                "routing store collections are invalid"
            )
        loaded_routes: dict[
            str, dict[str, Any]
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
                "domain_route": item.get("domain_route"),
            }
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
            loaded_executions[
                evidence.execution_id
            ] = evidence
        self._routes = loaded_routes
        self._executions = loaded_executions

    def route(
        self,
        request: ComputeRouteRequest,
        candidates: Sequence[ComputeCandidate],
        policy: ComputeRoutingPolicy,
        *,
        as_of: str,
        voc_evidence: ValueOfComputationEvidence | None = None,
        domain_route: RouteRecommendation | None = None,
    ) -> ComputeRouteDecision:
        decision = route_compute(
            request,
            candidates,
            policy,
            as_of=as_of,
            voc_evidence=voc_evidence,
            domain_route=domain_route,
        )
        domain_payload = None
        if domain_route is not None:
            domain_payload = {
                "status": domain_route.status.value,
                "reason": domain_route.reason,
                "observation_id": (
                    domain_route.observation_id
                ),
                "domain_profile": (
                    domain_route.domain_profile.value
                ),
            }
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
            "domain_route": domain_payload,
        }
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
        self._persist(staged, self._executions)
        self._routes = staged
        return decision

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
        decision = ComputeRouteDecision.from_payload(
            record["decision"]
        )
        if decision.tier is ComputeTier.WAIT:
            raise ModelComputeRouterError(
                "WAIT decision cannot record compute execution"
            )
        now = _instant("as_of", as_of)
        completed = _instant(
            "completed_at", completed_at
        )
        available = _instant(
            "available_at", available_at
        )
        if now < available:
            raise ModelComputeRouterError(
                "execution evidence is not yet "
                "causally available"
            )
        identity_matches = (
            backend_id == decision.backend_id
            and model_id == decision.model_id
            and config_sha256 == decision.config_sha256
        )
        if not identity_matches:
            disposition = (
                ExecutionDisposition.REJECTED_IDENTITY
            )
            reason = (
                "execution backend/model/config identity "
                "differs from routed decision"
            )
        elif completed > _instant(
            "decision_deadline",
            request.decision_deadline,
        ):
            disposition = (
                ExecutionDisposition.REJECTED_LATE
            )
            reason = (
                "execution completed after decision deadline"
            )
        elif (
            _seconds(now, completed)
            > request.response_ttl_seconds
        ):
            disposition = (
                ExecutionDisposition.REJECTED_STALE
            )
            reason = (
                "execution response exceeded request "
                "response TTL"
            )
        else:
            disposition = ExecutionDisposition.ACCEPTED
            reason = (
                "execution identity, deadline, availability "
                "and freshness are valid"
            )
        evidence = ComputeExecutionEvidence(
            execution_id=execution_id,
            decision_id=decision.decision_id,
            completed_at=completed_at,
            available_at=available_at,
            backend_id=backend_id,
            model_id=model_id,
            config_sha256=config_sha256,
            actual_cost=actual_cost,
            actual_latency_seconds=(
                actual_latency_seconds
            ),
            disposition=disposition,
            reason=reason,
            evidence_sha256=evidence_sha256,
        )
        existing = self._executions.get(execution_id)
        if existing is not None:
            if existing != evidence:
                raise ModelComputeRouterError(
                    "immutable execution id conflicts "
                    "with stored evidence"
                )
            return evidence
        staged = dict(self._executions)
        staged[execution_id] = evidence
        self._persist(self._routes, staged)
        self._executions = staged
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
