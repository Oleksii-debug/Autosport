from __future__ import annotations

"""Fail-closed decision-time aggregate for economically applicable costs.

Schema v2 deliberately cannot represent COMPLETE or positive monetary truth.  The
public resolver is installed from a closure seal that captures the exact canonical
input types, model-money resolver, output types, and immutable semantic tables at
module initialization.  Authority-bearing resolution therefore does not consult
caller-writable module globals after installation.
"""

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
    ProspectiveModelComputeMoneyReason,
    ProspectiveModelComputeMoneyStatus,
    resolve_prospective_model_compute_money,
)


_SCHEMA_VERSION = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_SOURCE_FAMILY = "autosport.prospective_model_compute_money"


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


# Public/debug mirrors.  The authority-bearing resolver below captures immutable
# copies and never reads these mappings after installation.
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


_COMPONENT_SEMANTICS: dict[
    CostClass,
    tuple[
        ProspectiveCostResolutionStatus,
        ProspectiveApplicableCostReason,
        tuple[ProspectiveCostDependencyAxis, ...],
        str | None,
    ],
] = {
    CostClass.MODEL_COMPUTE_AI: (
        ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN,
        ProspectiveApplicableCostReason.MODEL_COMPUTE_AUTHORITY_UNRESOLVED,
        (),
        _MODEL_SOURCE_FAMILY,
    ),
    CostClass.PROVIDER_DATA: (
        ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN,
        ProspectiveApplicableCostReason.NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY,
        (),
        None,
    ),
    CostClass.FIXED_CAMPAIGN: (
        ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN,
        ProspectiveApplicableCostReason.NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY,
        (),
        None,
    ),
    CostClass.EXECUTION_SLIPPAGE: (
        ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT,
        ProspectiveApplicableCostReason.EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION,
        (ProspectiveCostDependencyAxis.EXECUTION_STATE,),
        None,
    ),
    CostClass.EXECUTION_FEES_COMMISSION_TAX: (
        ProspectiveCostResolutionStatus.EXECUTION_AND_TERMINAL_STATE_DEPENDENT,
        ProspectiveApplicableCostReason.EXECUTION_FEES_DEPEND_ON_EXECUTION_OR_TERMINAL_STATE,
        (
            ProspectiveCostDependencyAxis.EXECUTION_STATE,
            ProspectiveCostDependencyAxis.TERMINAL_STATE,
        ),
        None,
    ),
}


@dataclass(frozen=True, slots=True, init=False)
class ProspectiveApplicableCostComponent:
    """Schema-v2 assertion/transport component; public construction is disabled."""

    cost_class: CostClass
    status: ProspectiveCostResolutionStatus
    reason: ProspectiveApplicableCostReason
    dependency_axes: tuple[ProspectiveCostDependencyAxis, ...]
    source_family: str | None
    source_evidence_id: str | None
    source_sha256: str | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ProspectiveApplicableCostError(
            "prospective applicable-cost components are resolver-owned"
        )

    def __post_init__(self) -> None:
        # Compatibility/local-shape validation only.  Authority paths use the
        # closure-sealed validator installed below and do not trust these globals.
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

        expected = _COMPONENT_SEMANTICS.get(self.cost_class)
        if expected is None:
            raise ProspectiveApplicableCostError(
                "cost_class has no schema-v2 prospective semantic authority"
            )
        expected_status, expected_reason, expected_axes, expected_source_family = expected
        if (
            self.status is not expected_status
            or self.reason is not expected_reason
            or self.dependency_axes != expected_axes
        ):
            raise ProspectiveApplicableCostError(
                "cost component does not match the canonical schema-v2 semantic tuple"
            )

        refs = (self.source_family, self.source_evidence_id, self.source_sha256)
        if expected_source_family is None:
            if any(value is not None for value in refs):
                raise ProspectiveApplicableCostError(
                    "this cost class cannot carry source authority in schema v2"
                )
            return

        if self.source_family != expected_source_family:
            raise ProspectiveApplicableCostError(
                "cost component source family does not match canonical authority"
            )
        if self.source_evidence_id is None or self.source_sha256 is None:
            raise ProspectiveApplicableCostError(
                "canonical source authority requires evidence id and SHA-256"
            )
        evidence_id = _sha256(self.source_evidence_id, "source_evidence_id")
        source_sha256 = _sha256(self.source_sha256, "source_sha256")
        if evidence_id != source_sha256:
            raise ProspectiveApplicableCostError(
                "schema-v2 model source evidence id and source SHA-256 must match"
            )

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


@dataclass(frozen=True, slots=True, init=False)
class ProspectiveApplicableCostResolution:
    """Schema-v2 assertion/transport aggregate; public construction is disabled."""

    intent_sha256: str
    opportunity_id: str
    portfolio_plan_sha256: str
    decision_at: datetime
    components: tuple[ProspectiveApplicableCostComponent, ...]
    completeness: ProspectiveApplicableCostCompleteness
    total_subtractable_amount: None
    currency: None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ProspectiveApplicableCostError(
            "prospective applicable-cost resolutions are resolver-owned"
        )

    def __post_init__(self) -> None:
        # Compatibility/local-shape validation only.  Authority paths use the
        # closure-sealed validator installed below and do not trust these globals.
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
        for item in self.components:
            item.__post_init__()
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


def _build_canonical_authority():
    """Capture exact executable/type/semantic authority outside module globals."""

    error_cls = ProspectiveApplicableCostError
    intent_cls = OpportunityIntent
    plan_cls = PortfolioPlan
    router_store_cls = ModelComputeRouterStore
    model_evidence_cls = ProspectiveModelComputeMoneyEvidence
    model_status_cls = ProspectiveModelComputeMoneyStatus
    model_reason_cls = ProspectiveModelComputeMoneyReason
    model_resolver = resolve_prospective_model_compute_money
    cost_class_cls = CostClass
    component_cls = ProspectiveApplicableCostComponent
    resolution_cls = ProspectiveApplicableCostResolution
    resolution_status_cls = ProspectiveCostResolutionStatus
    dependency_axis_cls = ProspectiveCostDependencyAxis
    completeness_cls = ProspectiveApplicableCostCompleteness
    reason_cls = ProspectiveApplicableCostReason
    datetime_cls = datetime
    timezone_utc = timezone.utc
    sha_pattern = re.compile(r"^[0-9a-f]{64}$")
    model_source_family = "autosport.prospective_model_compute_money"
    required_classes = tuple(sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value))

    status_axes = (
        (resolution_status_cls.UNKNOWN_UNPROVEN, ()),
        (
            resolution_status_cls.EXECUTION_STATE_DEPENDENT,
            (dependency_axis_cls.EXECUTION_STATE,),
        ),
        (
            resolution_status_cls.TERMINAL_STATE_DEPENDENT,
            (dependency_axis_cls.TERMINAL_STATE,),
        ),
        (
            resolution_status_cls.EXECUTION_AND_TERMINAL_STATE_DEPENDENT,
            (
                dependency_axis_cls.EXECUTION_STATE,
                dependency_axis_cls.TERMINAL_STATE,
            ),
        ),
    )
    semantics = (
        (
            cost_class_cls.MODEL_COMPUTE_AI,
            resolution_status_cls.UNKNOWN_UNPROVEN,
            reason_cls.MODEL_COMPUTE_AUTHORITY_UNRESOLVED,
            (),
            model_source_family,
        ),
        (
            cost_class_cls.PROVIDER_DATA,
            resolution_status_cls.UNKNOWN_UNPROVEN,
            reason_cls.NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY,
            (),
            None,
        ),
        (
            cost_class_cls.FIXED_CAMPAIGN,
            resolution_status_cls.UNKNOWN_UNPROVEN,
            reason_cls.NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY,
            (),
            None,
        ),
        (
            cost_class_cls.EXECUTION_SLIPPAGE,
            resolution_status_cls.EXECUTION_STATE_DEPENDENT,
            reason_cls.EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION,
            (dependency_axis_cls.EXECUTION_STATE,),
            None,
        ),
        (
            cost_class_cls.EXECUTION_FEES_COMMISSION_TAX,
            resolution_status_cls.EXECUTION_AND_TERMINAL_STATE_DEPENDENT,
            reason_cls.EXECUTION_FEES_DEPEND_ON_EXECUTION_OR_TERMINAL_STATE,
            (
                dependency_axis_cls.EXECUTION_STATE,
                dependency_axis_cls.TERMINAL_STATE,
            ),
            None,
        ),
    )

    def text(value: object, field: str) -> str:
        if type(value) is not str or not value or value != value.strip() or "\x00" in value:
            raise error_cls(f"{field} must be a non-empty canonical string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise error_cls(f"{field} must be valid UTF-8") from exc
        return value

    def sha256(value: object, field: str) -> str:
        digest = text(value, field)
        if sha_pattern.fullmatch(digest) is None:
            raise error_cls(f"{field} must be canonical lowercase SHA-256")
        return digest

    def instant(value: object, field: str) -> datetime:
        if isinstance(value, datetime_cls):
            parsed = value
        elif type(value) is str:
            raw = text(value, field)
            try:
                parsed = datetime_cls.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise error_cls(f"{field} must be valid ISO-8601") from exc
        else:
            raise error_cls(
                f"{field} must be a timezone-aware datetime/ISO-8601 string"
            )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise error_cls(f"{field} must be timezone-aware")
        return parsed.astimezone(timezone_utc)

    def time_text(value: datetime) -> str:
        return value.astimezone(timezone_utc).isoformat().replace("+00:00", "Z")

    def digest(payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def expected_axes(status: ProspectiveCostResolutionStatus):
        for candidate, axes in status_axes:
            if status is candidate:
                return axes
        raise error_cls("status has no sealed schema-v2 dependency semantics")

    def expected_semantics(cost_class: CostClass):
        for row in semantics:
            if cost_class is row[0]:
                return row[1:]
        raise error_cls("cost_class has no sealed schema-v2 prospective semantic authority")

    def validate_component(value: object) -> None:
        if type(value) is not component_cls:
            raise error_cls(
                "components must contain exact ProspectiveApplicableCostComponent values"
            )
        cost_class = object.__getattribute__(value, "cost_class")
        status = object.__getattribute__(value, "status")
        reason = object.__getattribute__(value, "reason")
        axes = object.__getattribute__(value, "dependency_axes")
        source_family = object.__getattribute__(value, "source_family")
        source_evidence_id = object.__getattribute__(value, "source_evidence_id")
        source_sha256 = object.__getattribute__(value, "source_sha256")

        if type(cost_class) is not cost_class_cls:
            raise error_cls("cost_class must be exact CostClass")
        if type(status) is not resolution_status_cls:
            raise error_cls("status must be exact ProspectiveCostResolutionStatus")
        if type(reason) is not reason_cls:
            raise error_cls("reason must be exact ProspectiveApplicableCostReason")
        if type(axes) is not tuple or any(type(axis) is not dependency_axis_cls for axis in axes):
            raise error_cls(
                "dependency_axes must be a tuple of exact ProspectiveCostDependencyAxis values"
            )
        if axes != expected_axes(status):
            raise error_cls(
                "status and dependency_axes must describe the same dependency dimensions"
            )
        expected_status, expected_reason, expected_component_axes, expected_source = (
            expected_semantics(cost_class)
        )
        if (
            status is not expected_status
            or reason is not expected_reason
            or axes != expected_component_axes
        ):
            raise error_cls(
                "cost component does not match the sealed canonical schema-v2 semantic tuple"
            )
        refs = (source_family, source_evidence_id, source_sha256)
        if expected_source is None:
            if any(item is not None for item in refs):
                raise error_cls(
                    "this cost class cannot carry source authority in schema v2"
                )
            return
        if source_family != expected_source:
            raise error_cls("cost component source family does not match canonical authority")
        if source_evidence_id is None or source_sha256 is None:
            raise error_cls("canonical source authority requires evidence id and SHA-256")
        if sha256(source_evidence_id, "source_evidence_id") != sha256(
            source_sha256, "source_sha256"
        ):
            raise error_cls(
                "schema-v2 model source evidence id and source SHA-256 must match"
            )

    def validate_resolution(value: object) -> None:
        if type(value) is not resolution_cls:
            raise error_cls(
                "prospective applicable-cost resolution must use the exact canonical type"
            )
        intent_sha256 = object.__getattribute__(value, "intent_sha256")
        opportunity_id = object.__getattribute__(value, "opportunity_id")
        plan_sha256 = object.__getattribute__(value, "portfolio_plan_sha256")
        decision_at = object.__getattribute__(value, "decision_at")
        components = object.__getattribute__(value, "components")
        completeness = object.__getattribute__(value, "completeness")
        amount = object.__getattribute__(value, "total_subtractable_amount")
        currency = object.__getattribute__(value, "currency")

        sha256(intent_sha256, "intent_sha256")
        text(opportunity_id, "opportunity_id")
        sha256(plan_sha256, "portfolio_plan_sha256")
        normalized = instant(decision_at, "decision_at")
        if normalized != decision_at:
            raise error_cls("decision_at must be canonical UTC")
        if type(components) is not tuple:
            raise error_cls("components must be a canonical tuple")
        for component in components:
            validate_component(component)
        classes = tuple(object.__getattribute__(item, "cost_class") for item in components)
        if classes != required_classes:
            raise error_cls(
                "components must contain each required cost class exactly once in canonical order"
            )
        if (
            type(completeness) is not completeness_cls
            or completeness is not completeness_cls.INCOMPLETE
        ):
            raise error_cls("schema v2 cannot represent COMPLETE applicable-cost authority")
        if amount is not None or currency is not None:
            raise error_cls("schema v2 cannot represent an authoritative monetary total")

    def model_evidence_id(value: object, *, expected_request_id: str) -> str:
        if type(value) is not model_evidence_cls:
            raise error_cls("model-compute authority returned a non-canonical evidence type")
        intent_sha = sha256(object.__getattribute__(value, "intent_sha256"), "model intent_sha256")
        opportunity_id = text(
            object.__getattribute__(value, "opportunity_id"), "model opportunity_id"
        )
        request_id = text(object.__getattribute__(value, "request_id"), "model request_id")
        decision_at = instant(object.__getattribute__(value, "decision_at"), "model decision_at")
        router_decided_at = instant(
            object.__getattribute__(value, "router_decided_at"), "model router_decided_at"
        )
        request_sha = sha256(
            object.__getattribute__(value, "router_request_sha256"),
            "model router_request_sha256",
        )
        decision_sha = sha256(
            object.__getattribute__(value, "router_decision_sha256"),
            "model router_decision_sha256",
        )
        status = object.__getattribute__(value, "status")
        reason = object.__getattribute__(value, "reason")
        amount = object.__getattribute__(value, "amount")
        currency = object.__getattribute__(value, "currency")
        tariff = object.__getattribute__(value, "tariff_sha256")
        if request_id != expected_request_id:
            raise error_cls("model-compute evidence request mismatch")
        if router_decided_at > decision_at:
            raise error_cls("model-compute evidence router decision is from the future")
        if type(status) is not model_status_cls or status is not model_status_cls.UNKNOWN_UNPROVEN:
            raise error_cls("aggregate schema v2 cannot consume positive model-compute money")
        if type(reason) is not model_reason_cls:
            raise error_cls("model-compute evidence reason is non-canonical")
        if amount is not None or currency is not None or tariff is not None:
            raise error_cls("aggregate schema v2 cannot consume positive model-compute money")
        payload = {
            "schema": "autosport.prospective_model_compute_money",
            "schema_version": 1,
            "intent_sha256": intent_sha,
            "opportunity_id": opportunity_id,
            "request_id": request_id,
            "decision_at": time_text(decision_at),
            "router_decided_at": time_text(router_decided_at),
            "router_request_sha256": request_sha,
            "router_decision_sha256": decision_sha,
            "status": status.value,
            "reason": reason.value,
            "amount": None,
            "currency": None,
            "tariff_sha256": None,
        }
        return digest(payload)

    def resolve(
        *,
        intent: OpportunityIntent,
        plan: PortfolioPlan,
        router_store: ModelComputeRouterStore,
        model_request_id: str,
        decision_at: datetime,
    ) -> ProspectiveApplicableCostResolution:
        """Re-resolve sealed product-owned prospective cost truth and fail closed."""

        if type(intent) is not intent_cls:
            raise error_cls("intent must be the exact canonical OpportunityIntent type")
        if type(plan) is not plan_cls:
            raise error_cls("plan must be the exact canonical PortfolioPlan type")
        if type(router_store) is not router_store_cls:
            raise error_cls(
                "router_store must be the exact canonical ModelComputeRouterStore type"
            )

        cutoff = instant(
            getattr(intent.risk_context, "proposal_ts", None),
            "intent risk_context proposal_ts",
        )
        asserted_cutoff = instant(decision_at, "decision_at")
        if asserted_cutoff != cutoff:
            raise error_cls(
                "caller decision_at does not match canonical OpportunityIntent proposal_ts"
            )
        plan_cutoff = instant(plan.decision_ts, "portfolio plan decision_ts")
        if plan_cutoff != cutoff:
            raise error_cls(
                "portfolio plan decision_ts does not match OpportunityIntent proposal_ts"
            )

        intent_sha256 = sha256(intent.intent_sha256, "intent.intent_sha256")
        intent_id = text(getattr(intent, "intent_id", None), "intent.intent_id")
        if type(plan.intent_sha256s) is not tuple or type(plan.intent_ids) is not tuple:
            raise error_cls("canonical PortfolioPlan intent identity vectors must be tuples")
        matches = [
            index
            for index, candidate_digest in enumerate(plan.intent_sha256s)
            if candidate_digest == intent_sha256
        ]
        if len(matches) != 1:
            raise error_cls(
                "canonical PortfolioPlan must bind the exact intent_sha256 exactly once"
            )
        index = matches[0]
        if index >= len(plan.intent_ids) or plan.intent_ids[index] != intent_id:
            raise error_cls("canonical PortfolioPlan intent_id/intent_sha256 binding mismatch")
        portfolio_plan_sha256 = sha256(plan.plan_sha256, "portfolio plan plan_sha256")
        canonical_request_id = text(model_request_id, "model_request_id")

        model_evidence = model_resolver(
            intent=intent,
            router_store=router_store,
            request_id=canonical_request_id,
            decision_at=cutoff,
        )
        evidence_id = model_evidence_id(
            model_evidence,
            expected_request_id=canonical_request_id,
        )
        if sha256(
            object.__getattribute__(model_evidence, "intent_sha256"),
            "model intent_sha256",
        ) != intent_sha256:
            raise error_cls("model-compute evidence intent mismatch")
        opportunity_id = text(
            getattr(intent.opportunity, "opportunity_id", None),
            "intent opportunity_id",
        )
        if text(
            object.__getattribute__(model_evidence, "opportunity_id"),
            "model opportunity_id",
        ) != opportunity_id:
            raise error_cls("model-compute evidence opportunity mismatch")
        if instant(
            object.__getattribute__(model_evidence, "decision_at"),
            "model decision_at",
        ) != cutoff:
            raise error_cls("model-compute evidence decision cutoff mismatch")

        def issue_component(
            cost_class: CostClass,
            reason: ProspectiveApplicableCostReason,
            *,
            status: ProspectiveCostResolutionStatus = resolution_status_cls.UNKNOWN_UNPROVEN,
            source_family: str | None = None,
            source_evidence_id: str | None = None,
            source_sha256: str | None = None,
        ) -> ProspectiveApplicableCostComponent:
            component = object.__new__(component_cls)
            object.__setattr__(component, "cost_class", cost_class)
            object.__setattr__(component, "status", status)
            object.__setattr__(component, "reason", reason)
            object.__setattr__(component, "dependency_axes", expected_axes(status))
            object.__setattr__(component, "source_family", source_family)
            object.__setattr__(component, "source_evidence_id", source_evidence_id)
            object.__setattr__(component, "source_sha256", source_sha256)
            validate_component(component)
            return component

        components = {
            cost_class_cls.MODEL_COMPUTE_AI: issue_component(
                cost_class_cls.MODEL_COMPUTE_AI,
                reason_cls.MODEL_COMPUTE_AUTHORITY_UNRESOLVED,
                source_family=model_source_family,
                source_evidence_id=evidence_id,
                source_sha256=evidence_id,
            ),
            cost_class_cls.PROVIDER_DATA: issue_component(
                cost_class_cls.PROVIDER_DATA,
                reason_cls.NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY,
            ),
            cost_class_cls.FIXED_CAMPAIGN: issue_component(
                cost_class_cls.FIXED_CAMPAIGN,
                reason_cls.NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY,
            ),
            cost_class_cls.EXECUTION_SLIPPAGE: issue_component(
                cost_class_cls.EXECUTION_SLIPPAGE,
                reason_cls.EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION,
                status=resolution_status_cls.EXECUTION_STATE_DEPENDENT,
            ),
            cost_class_cls.EXECUTION_FEES_COMMISSION_TAX: issue_component(
                cost_class_cls.EXECUTION_FEES_COMMISSION_TAX,
                reason_cls.EXECUTION_FEES_DEPEND_ON_EXECUTION_OR_TERMINAL_STATE,
                status=resolution_status_cls.EXECUTION_AND_TERMINAL_STATE_DEPENDENT,
            ),
        }
        ordered = tuple(components[cost_class] for cost_class in required_classes)

        resolution = object.__new__(resolution_cls)
        object.__setattr__(resolution, "intent_sha256", intent_sha256)
        object.__setattr__(resolution, "opportunity_id", opportunity_id)
        object.__setattr__(resolution, "portfolio_plan_sha256", portfolio_plan_sha256)
        object.__setattr__(resolution, "decision_at", cutoff)
        object.__setattr__(resolution, "components", ordered)
        object.__setattr__(resolution, "completeness", completeness_cls.INCOMPLETE)
        object.__setattr__(resolution, "total_subtractable_amount", None)
        object.__setattr__(resolution, "currency", None)
        validate_resolution(resolution)
        return resolution

    return resolve, validate_component, validate_resolution, (
        error_cls,
        component_cls,
        resolution_cls,
        intent_cls,
        plan_cls,
        router_store_cls,
    )


(
    resolve_prospective_applicable_costs,
    _SEALED_COMPONENT_VALIDATOR,
    _SEALED_RESOLUTION_VALIDATOR,
    _SEALED_CANONICAL_TYPES,
) = _build_canonical_authority()
