from __future__ import annotations

"""Fail-closed decision-time aggregate for economically applicable costs."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
from typing import Any

from .campaign_cost_evidence import CostClass, REQUIRED_COST_CLASSES
from .model_compute_router import ModelComputeRouterStore
from .portfolio_plan import OpportunityIntent, PortfolioPlan
from .prospective_model_compute_money import (
    ProspectiveModelComputeMoneyEvidence,
    ProspectiveModelComputeMoneyStatus,
    resolve_prospective_model_compute_money,
)


_SCHEMA_VERSION = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProspectiveApplicableCostError(ValueError):
    """Raised when canonical aggregate cost evidence is invalid."""


class ProspectiveCostResolutionStatus(StrEnum):
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"
    EXECUTION_STATE_DEPENDENT = "EXECUTION_STATE_DEPENDENT"
    TERMINAL_STATE_DEPENDENT = "TERMINAL_STATE_DEPENDENT"
    EXECUTION_AND_TERMINAL_STATE_DEPENDENT = "EXECUTION_AND_TERMINAL_STATE_DEPENDENT"


class ProspectiveCostDependencyAxis(StrEnum):
    EXECUTION_STATE = "EXECUTION_STATE"
    TERMINAL_STATE = "TERMINAL_STATE"


class ProspectiveApplicableCostCompleteness(StrEnum):
    """Schema v2 deliberately has no COMPLETE state."""

    INCOMPLETE = "INCOMPLETE"


class ProspectiveApplicableCostReason(StrEnum):
    MODEL_COMPUTE_AUTHORITY_UNRESOLVED = "MODEL_COMPUTE_AUTHORITY_UNRESOLVED"
    NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY = "NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY"
    NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY = (
        "NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY"
    )
    EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION = "EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION"
    EXECUTION_FEES_DEPEND_ON_EXECUTION_OR_TERMINAL_STATE = (
        "EXECUTION_FEES_DEPEND_ON_EXECUTION_OR_TERMINAL_STATE"
    )


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProspectiveApplicableCostError(
            f"{field} must be a non-empty canonical string"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProspectiveApplicableCostError(f"{field} must be valid UTF-8") from exc
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ProspectiveApplicableCostError(
            f"{field} must be canonical lowercase SHA-256"
        )
    return digest


def _instant(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        raw = _text(value, field)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProspectiveApplicableCostError(
                f"{field} must be valid ISO-8601"
            ) from exc
    else:
        raise ProspectiveApplicableCostError(
            f"{field} must be a timezone-aware datetime/ISO-8601 string"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProspectiveApplicableCostError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_STATUS_AXES: dict[
    ProspectiveCostResolutionStatus, tuple[ProspectiveCostDependencyAxis, ...]
] = {
    ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN: (),
    ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT: (
        ProspectiveCostDependencyAxis.EXECUTION_STATE,
    ),
    ProspectiveCostResolutionStatus.TERMINAL_STATE_DEPENDENT: (
        ProspectiveCostDependencyAxis.TERMINAL_STATE,
    ),
    ProspectiveCostResolutionStatus.EXECUTION_AND_TERMINAL_STATE_DEPENDENT: (
        ProspectiveCostDependencyAxis.EXECUTION_STATE,
        ProspectiveCostDependencyAxis.TERMINAL_STATE,
    ),
}


@dataclass(frozen=True, slots=True)
class ProspectiveApplicableCostComponent:
    cost_class: CostClass
    status: ProspectiveCostResolutionStatus
    reason: ProspectiveApplicableCostReason
    dependency_axes: tuple[ProspectiveCostDependencyAxis, ...] = ()
    source_family: str | None = None
    source_evidence_id: str | None = None
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.cost_class) is not CostClass:
            raise ProspectiveApplicableCostError("cost_class must be exact CostClass")
        if type(self.status) is not ProspectiveCostResolutionStatus:
            raise ProspectiveApplicableCostError(
                "status must be exact ProspectiveCostResolutionStatus"
            )
        if type(self.reason) is not ProspectiveApplicableCostReason:
            raise ProspectiveApplicableCostError(
                "reason must be exact ProspectiveApplicableCostReason"
            )
        if type(self.dependency_axes) is not tuple or any(
            type(axis) is not ProspectiveCostDependencyAxis
            for axis in self.dependency_axes
        ):
            raise ProspectiveApplicableCostError(
                "dependency_axes must be a tuple of exact ProspectiveCostDependencyAxis values"
            )
        if self.dependency_axes != _STATUS_AXES[self.status]:
            raise ProspectiveApplicableCostError(
                "status and dependency_axes must describe the same dependency dimensions"
            )
        refs = (self.source_family, self.source_evidence_id, self.source_sha256)
        if any(value is None for value in refs):
            if any(value is not None for value in refs):
                raise ProspectiveApplicableCostError(
                    "source authority fields must be all present or all absent"
                )
        else:
            _text(self.source_family, "source_family")
            _sha256(self.source_evidence_id, "source_evidence_id")
            _sha256(self.source_sha256, "source_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost_class": self.cost_class.value,
            "status": self.status.value,
            "dependency_axes": [axis.value for axis in self.dependency_axes],
            "reason": self.reason.value,
            "amount": None,
            "currency": None,
            "source_family": self.source_family,
            "source_evidence_id": self.source_evidence_id,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProspectiveApplicableCostResolution:
    intent_sha256: str
    opportunity_id: str
    portfolio_plan_sha256: str
    decision_at: datetime
    components: tuple[ProspectiveApplicableCostComponent, ...]
    completeness: ProspectiveApplicableCostCompleteness = (
        ProspectiveApplicableCostCompleteness.INCOMPLETE
    )
    total_subtractable_amount: None = None
    currency: None = None

    def __post_init__(self) -> None:
        _sha256(self.intent_sha256, "intent_sha256")
        _text(self.opportunity_id, "opportunity_id")
        _sha256(self.portfolio_plan_sha256, "portfolio_plan_sha256")
        normalized_cutoff = _instant(self.decision_at, "decision_at")
        if normalized_cutoff != self.decision_at:
            raise ProspectiveApplicableCostError("decision_at must be canonical UTC")
        if type(self.components) is not tuple:
            raise ProspectiveApplicableCostError("components must be a canonical tuple")
        if any(type(item) is not ProspectiveApplicableCostComponent for item in self.components):
            raise ProspectiveApplicableCostError(
                "components must contain exact ProspectiveApplicableCostComponent values"
            )
        classes = tuple(item.cost_class for item in self.components)
        expected = tuple(sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value))
        if classes != expected:
            raise ProspectiveApplicableCostError(
                "components must contain each required cost class exactly once in canonical order"
            )
        if (
            type(self.completeness) is not ProspectiveApplicableCostCompleteness
            or self.completeness is not ProspectiveApplicableCostCompleteness.INCOMPLETE
        ):
            raise ProspectiveApplicableCostError(
                "schema v2 cannot represent COMPLETE applicable-cost authority"
            )
        if self.total_subtractable_amount is not None or self.currency is not None:
            raise ProspectiveApplicableCostError(
                "schema v2 cannot represent an authoritative monetary total"
            )

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_dict(include_evidence_id=False))

    def to_dict(self, *, include_evidence_id: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "autosport.prospective_applicable_cost",
            "schema_version": _SCHEMA_VERSION,
            "intent_sha256": self.intent_sha256,
            "opportunity_id": self.opportunity_id,
            "portfolio_plan_sha256": self.portfolio_plan_sha256,
            "decision_at": _time_text(self.decision_at),
            "components": [item.to_dict() for item in self.components],
            "completeness": self.completeness.value,
            "total_subtractable_amount": None,
            "currency": None,
        }
        if include_evidence_id:
            payload["evidence_id"] = _digest(payload)
        return payload


def _unresolved(
    cost_class: CostClass,
    reason: ProspectiveApplicableCostReason,
    *,
    status: ProspectiveCostResolutionStatus = (
        ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN
    ),
    source_family: str | None = None,
    source_evidence_id: str | None = None,
    source_sha256: str | None = None,
) -> ProspectiveApplicableCostComponent:
    return ProspectiveApplicableCostComponent(
        cost_class=cost_class,
        status=status,
        dependency_axes=_STATUS_AXES[status],
        reason=reason,
        source_family=source_family,
        source_evidence_id=source_evidence_id,
        source_sha256=source_sha256,
    )


def resolve_prospective_applicable_costs(
    *,
    intent: OpportunityIntent,
    plan: PortfolioPlan,
    router_store: ModelComputeRouterStore,
    model_request_id: str,
    decision_at: datetime,
) -> ProspectiveApplicableCostResolution:
    """Re-resolve available product-owned prospective cost truth and fail closed."""

    if type(intent) is not OpportunityIntent:
        raise ProspectiveApplicableCostError(
            "intent must be the exact canonical OpportunityIntent type"
        )
    if type(plan) is not PortfolioPlan:
        raise ProspectiveApplicableCostError(
            "plan must be the exact canonical PortfolioPlan type"
        )
    if type(router_store) is not ModelComputeRouterStore:
        raise ProspectiveApplicableCostError(
            "router_store must be the exact canonical ModelComputeRouterStore type"
        )

    cutoff = _instant(
        getattr(intent.risk_context, "proposal_ts", None),
        "intent risk_context proposal_ts",
    )
    asserted_cutoff = _instant(decision_at, "decision_at")
    if asserted_cutoff != cutoff:
        raise ProspectiveApplicableCostError(
            "caller decision_at does not match canonical OpportunityIntent proposal_ts"
        )
    plan_cutoff = _instant(plan.decision_ts, "portfolio plan decision_ts")
    if plan_cutoff != cutoff:
        raise ProspectiveApplicableCostError(
            "portfolio plan decision_ts does not match OpportunityIntent proposal_ts"
        )

    intent_sha256 = _sha256(intent.intent_sha256, "intent.intent_sha256")
    intent_id = _text(getattr(intent, "intent_id", None), "intent.intent_id")
    if type(plan.intent_sha256s) is not tuple or type(plan.intent_ids) is not tuple:
        raise ProspectiveApplicableCostError(
            "canonical PortfolioPlan intent identity vectors must be tuples"
        )
    matches = [
        index
        for index, digest in enumerate(plan.intent_sha256s)
        if digest == intent_sha256
    ]
    if len(matches) != 1:
        raise ProspectiveApplicableCostError(
            "canonical PortfolioPlan must bind the exact intent_sha256 exactly once"
        )
    index = matches[0]
    if index >= len(plan.intent_ids) or plan.intent_ids[index] != intent_id:
        raise ProspectiveApplicableCostError(
            "canonical PortfolioPlan intent_id/intent_sha256 binding mismatch"
        )
    portfolio_plan_sha256 = _sha256(
        plan.plan_sha256,
        "portfolio plan plan_sha256",
    )

    model_evidence = resolve_prospective_model_compute_money(
        intent=intent,
        router_store=router_store,
        request_id=_text(model_request_id, "model_request_id"),
        decision_at=cutoff,
    )
    if type(model_evidence) is not ProspectiveModelComputeMoneyEvidence:
        raise ProspectiveApplicableCostError(
            "model-compute authority returned a non-canonical evidence type"
        )
    if (
        type(model_evidence.status) is not ProspectiveModelComputeMoneyStatus
        or model_evidence.status is not ProspectiveModelComputeMoneyStatus.UNKNOWN_UNPROVEN
    ):
        raise ProspectiveApplicableCostError(
            "aggregate schema v2 cannot consume positive model-compute money"
        )
    model_evidence_id = _sha256(model_evidence.evidence_id, "model evidence_id")
    if _sha256(model_evidence.intent_sha256, "model intent_sha256") != intent_sha256:
        raise ProspectiveApplicableCostError("model-compute evidence intent mismatch")
    opportunity_id = _text(
        getattr(intent.opportunity, "opportunity_id", None),
        "intent opportunity_id",
    )
    if _text(model_evidence.opportunity_id, "model opportunity_id") != opportunity_id:
        raise ProspectiveApplicableCostError(
            "model-compute evidence opportunity mismatch"
        )
    if _instant(model_evidence.decision_at, "model decision_at") != cutoff:
        raise ProspectiveApplicableCostError(
            "model-compute evidence decision cutoff mismatch"
        )

    components = {
        CostClass.MODEL_COMPUTE_AI: _unresolved(
            CostClass.MODEL_COMPUTE_AI,
            ProspectiveApplicableCostReason.MODEL_COMPUTE_AUTHORITY_UNRESOLVED,
            source_family="autosport.prospective_model_compute_money",
            source_evidence_id=model_evidence_id,
            source_sha256=model_evidence_id,
        ),
        CostClass.PROVIDER_DATA: _unresolved(
            CostClass.PROVIDER_DATA,
            ProspectiveApplicableCostReason.NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY,
        ),
        CostClass.FIXED_CAMPAIGN: _unresolved(
            CostClass.FIXED_CAMPAIGN,
            ProspectiveApplicableCostReason.NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY,
        ),
        CostClass.EXECUTION_SLIPPAGE: _unresolved(
            CostClass.EXECUTION_SLIPPAGE,
            ProspectiveApplicableCostReason.EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION,
            status=ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT,
        ),
        CostClass.EXECUTION_FEES_COMMISSION_TAX: _unresolved(
            CostClass.EXECUTION_FEES_COMMISSION_TAX,
            ProspectiveApplicableCostReason.EXECUTION_FEES_DEPEND_ON_EXECUTION_OR_TERMINAL_STATE,
            status=ProspectiveCostResolutionStatus.EXECUTION_AND_TERMINAL_STATE_DEPENDENT,
        ),
    }
    ordered = tuple(
        components[cost_class]
        for cost_class in sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value)
    )
    return ProspectiveApplicableCostResolution(
        intent_sha256=intent_sha256,
        opportunity_id=opportunity_id,
        portfolio_plan_sha256=portfolio_plan_sha256,
        decision_at=cutoff,
        components=ordered,
    )
