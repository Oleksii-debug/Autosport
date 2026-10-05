from __future__ import annotations

"""Fail-closed decision-time aggregate for economically applicable costs.

Schema v3 still cannot represent COMPLETE or a positive monetary total. It can
represent one product-verified KNOWN_ZERO execution-slippage source while every
unresolved cost class remains fail-closed. The public resolver is installed from a closure seal that captures the exact canonical
input types, model-money resolver, output types, and immutable semantic tables at
module initialization.  Authority-bearing resolution therefore does not consult
caller-writable module globals after installation.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import re
from typing import Any

from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
    BetfairStandardLimitPriceBoundStatus,
)
from .betfair_standard_limit_price_bound_product_verifier import (
    verify_product_betfair_standard_limit_price_bound,
)
from .campaign_cost_evidence import CostClass, REQUIRED_COST_CLASSES
from .model_compute_router import (
    ComputeRouteDecision,
    ComputeRouteRequest,
    ModelComputeRouterStore,
)
from .opportunity import Opportunity
from .real_execution_ledger import RealExecutionLedger
from .risk import ProposedTicketRiskContext
from .supervised_plan_issuance import SupervisedPlanIssuanceStore
from .trusted_runtime_code_profile import TrustedRuntimeCodeProfile
from .portfolio_plan import OpportunityIntent, PortfolioPlan
from .prospective_model_compute_money import (
    ProspectiveModelComputeMoneyEvidence,
    ProspectiveModelComputeMoneyReason,
    ProspectiveModelComputeMoneyStatus,
    resolve_prospective_model_compute_money,
)


_SCHEMA_VERSION = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_SOURCE_FAMILY = "autosport.prospective_model_compute_money"
_BETFAIR_STANDARD_LIMIT_SOURCE_FAMILY = "autosport.betfair_standard_limit_price_bound"


class ProspectiveApplicableCostError(ValueError):
    """Raised when canonical aggregate cost evidence is invalid."""


class ProspectiveCostResolutionStatus(StrEnum):
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"
    KNOWN_ZERO = "KNOWN_ZERO"
    EXECUTION_STATE_DEPENDENT = "EXECUTION_STATE_DEPENDENT"
    TERMINAL_STATE_DEPENDENT = "TERMINAL_STATE_DEPENDENT"
    EXECUTION_AND_TERMINAL_STATE_DEPENDENT = "EXECUTION_AND_TERMINAL_STATE_DEPENDENT"


class ProspectiveCostDependencyAxis(StrEnum):
    EXECUTION_STATE = "EXECUTION_STATE"
    TERMINAL_STATE = "TERMINAL_STATE"


class ProspectiveApplicableCostCompleteness(StrEnum):
    """Schema v3 deliberately has no COMPLETE state."""

    INCOMPLETE = "INCOMPLETE"


class ProspectiveApplicableCostReason(StrEnum):
    MODEL_COMPUTE_AUTHORITY_UNRESOLVED = "MODEL_COMPUTE_AUTHORITY_UNRESOLVED"
    NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY = "NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY"
    NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY = (
        "NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY"
    )
    EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION = "EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION"
    BETFAIR_STANDARD_LIMIT_ZERO_ADVERSE_PRICE = (
        "BETFAIR_STANDARD_LIMIT_ZERO_ADVERSE_PRICE"
    )
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
    ProspectiveCostResolutionStatus.KNOWN_ZERO: (),
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
        (
            ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT,
            ProspectiveApplicableCostReason.EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION,
            (ProspectiveCostDependencyAxis.EXECUTION_STATE,),
            None,
        ),
        (
            ProspectiveCostResolutionStatus.KNOWN_ZERO,
            ProspectiveApplicableCostReason.BETFAIR_STANDARD_LIMIT_ZERO_ADVERSE_PRICE,
            (),
            _BETFAIR_STANDARD_LIMIT_SOURCE_FAMILY,
        ),
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
    """Schema-v3 assertion/transport component; public construction is disabled."""

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
                "cost_class has no schema-v3 prospective semantic authority"
            )
        candidates = (
            expected
            if self.cost_class is CostClass.EXECUTION_SLIPPAGE
            else (expected,)
        )
        matched = next(
            (
                candidate
                for candidate in candidates
                if self.status is candidate[0]
                and self.reason is candidate[1]
                and self.dependency_axes == candidate[2]
            ),
            None,
        )
        if matched is None:
            raise ProspectiveApplicableCostError(
                "cost component does not match the canonical schema-v3 semantic tuple"
            )
        expected_status, expected_reason, expected_axes, expected_source_family = matched
        if (
            self.status is not expected_status
            or self.reason is not expected_reason
            or self.dependency_axes != expected_axes
        ):
            raise ProspectiveApplicableCostError(
                "cost component does not match the canonical schema-v3 semantic tuple"
            )

        refs = (self.source_family, self.source_evidence_id, self.source_sha256)
        if expected_source_family is None:
            if any(value is not None for value in refs):
                raise ProspectiveApplicableCostError(
                    "this cost class cannot carry source authority in schema v3"
                )
            return

        canonical_source_family = _text(self.source_family, "source_family")
        if canonical_source_family != expected_source_family:
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
                "schema-v3 model source evidence id and source SHA-256 must match"
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
    """Schema-v3 assertion/transport aggregate; public construction is disabled."""

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
                "schema v3 cannot represent COMPLETE applicable-cost authority"
            )
        if self.total_subtractable_amount is not None or self.currency is not None:
            raise ProspectiveApplicableCostError(
                "schema v3 cannot represent an authoritative monetary total"
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
    intent_sha_getter = intent_cls.intent_sha256.fget
    intent_sha_getter_code = intent_sha_getter.__code__
    plan_sha_getter = plan_cls.plan_sha256.fget
    plan_sha_getter_code = plan_sha_getter.__code__
    opportunity_cls = Opportunity
    risk_context_cls = ProposedTicketRiskContext
    router_store_cls = ModelComputeRouterStore
    router_request_cls = ComputeRouteRequest
    router_decision_cls = ComputeRouteDecision
    router_get_request = router_store_cls.get_request
    router_get_request_code = router_get_request.__code__
    router_get_decision = router_store_cls.get_decision
    router_get_decision_code = router_get_decision.__code__
    router_request_payload = router_request_cls.payload
    router_request_payload_code = router_request_payload.__code__
    router_decision_payload = router_decision_cls.payload
    router_decision_payload_code = router_decision_payload.__code__
    model_evidence_cls = ProspectiveModelComputeMoneyEvidence
    model_status_cls = ProspectiveModelComputeMoneyStatus
    model_reason_cls = ProspectiveModelComputeMoneyReason
    model_resolver = resolve_prospective_model_compute_money
    model_resolver_code = model_resolver.__code__
    slippage_error_cls = BetfairStandardLimitPriceBoundError
    slippage_evidence_cls = BetfairStandardLimitPriceBoundEvidence
    slippage_status_cls = BetfairStandardLimitPriceBoundStatus
    slippage_product_verifier = verify_product_betfair_standard_limit_price_bound
    slippage_product_verifier_code = slippage_product_verifier.__code__
    ledger_cls = RealExecutionLedger
    issuance_store_cls = SupervisedPlanIssuanceStore
    runtime_profile_cls = TrustedRuntimeCodeProfile
    cost_class_cls = CostClass
    component_cls = ProspectiveApplicableCostComponent
    resolution_cls = ProspectiveApplicableCostResolution
    resolution_status_cls = ProspectiveCostResolutionStatus
    dependency_axis_cls = ProspectiveCostDependencyAxis
    completeness_cls = ProspectiveApplicableCostCompleteness
    reason_cls = ProspectiveApplicableCostReason
    datetime_cls = datetime
    decimal_cls = Decimal
    timezone_utc = timezone.utc
    sha_pattern = re.compile(r"^[0-9a-f]{64}$")
    json_dumps = json.dumps
    json_dumps_code = json_dumps.__code__
    sha256_constructor = hashlib.sha256
    model_source_family = "autosport.prospective_model_compute_money"
    slippage_source_family = "autosport.betfair_standard_limit_price_bound"
    required_classes = tuple(sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value))

    status_axes = (
        (resolution_status_cls.UNKNOWN_UNPROVEN, ()),
        (resolution_status_cls.KNOWN_ZERO, ()),
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
            cost_class_cls.EXECUTION_SLIPPAGE,
            resolution_status_cls.KNOWN_ZERO,
            reason_cls.BETFAIR_STANDARD_LIMIT_ZERO_ADVERSE_PRICE,
            (),
            slippage_source_family,
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
        if type(value) is datetime_cls:
            if object.__getattribute__(value, "tzinfo") is not timezone_utc:
                raise error_cls(f"{field} datetime input must use exact UTC timezone authority")
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
        if json_dumps.__code__ is not json_dumps_code:
            raise error_cls("canonical JSON proof serializer authority changed")
        encoded = json_dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if json_dumps.__code__ is not json_dumps_code:
            raise error_cls("canonical JSON proof serializer authority changed")
        return sha256_constructor(encoded).hexdigest()

    def expected_axes(status: ProspectiveCostResolutionStatus):
        for candidate, axes in status_axes:
            if status is candidate:
                return axes
        raise error_cls("status has no sealed schema-v3 dependency semantics")

    def expected_semantics(cost_class: CostClass, status, reason, axes):
        for row in semantics:
            if (
                cost_class is row[0]
                and status is row[1]
                and reason is row[2]
                and axes == row[3]
            ):
                return row[1:]
        raise error_cls(
            "cost component does not match the sealed canonical schema-v3 semantic tuple"
        )

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
            expected_semantics(cost_class, status, reason, axes)
        )
        if (
            status is not expected_status
            or reason is not expected_reason
            or axes != expected_component_axes
        ):
            raise error_cls(
                "cost component does not match the sealed canonical schema-v3 semantic tuple"
            )
        refs = (source_family, source_evidence_id, source_sha256)
        if expected_source is None:
            if any(item is not None for item in refs):
                raise error_cls(
                    "this cost class cannot carry source authority in schema v3"
                )
            return
        canonical_source_family = text(source_family, "source_family")
        if canonical_source_family != expected_source:
            raise error_cls("cost component source family does not match canonical authority")
        if source_evidence_id is None or source_sha256 is None:
            raise error_cls("canonical source authority requires evidence id and SHA-256")
        if sha256(source_evidence_id, "source_evidence_id") != sha256(
            source_sha256, "source_sha256"
        ):
            raise error_cls(
                "schema-v3 model source evidence id and source SHA-256 must match"
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
            raise error_cls("schema v3 cannot represent COMPLETE applicable-cost authority")
        if amount is not None or currency is not None:
            raise error_cls("schema v3 cannot represent an authoritative monetary total")

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
            raise error_cls("aggregate schema v3 cannot consume positive model-compute money")
        if type(reason) is not model_reason_cls:
            raise error_cls("model-compute evidence reason is non-canonical")
        if amount is not None or currency is not None or tariff is not None:
            raise error_cls("aggregate schema v3 cannot consume positive model-compute money")
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

    def slippage_evidence_id(value: object) -> str:
        if type(value) is not slippage_evidence_cls:
            raise error_cls("canonical Betfair slippage evidence type changed")

        requested_stake = object.__getattribute__(value, "requested_stake")
        price_floor_odds = object.__getattribute__(value, "price_floor_odds")
        if (
            type(requested_stake) is not decimal_cls
            or not requested_stake.is_finite()
            or requested_stake <= 0
        ):
            raise error_cls("slippage requested_stake must be an exact finite positive Decimal")
        if (
            type(price_floor_odds) is not decimal_cls
            or not price_floor_odds.is_finite()
            or price_floor_odds <= 0
        ):
            raise error_cls("slippage price_floor_odds must be an exact finite positive Decimal")

        matchme_proven = object.__getattribute__(value, "matchme_applicability_proven")
        zero_adverse = object.__getattribute__(value, "zero_adverse_price_deterioration")
        feasibility = object.__getattribute__(value, "execution_feasibility_proven")
        realized_exact = object.__getattribute__(value, "realized_price_exact")
        if (
            type(matchme_proven) is not bool
            or type(zero_adverse) is not bool
            or type(feasibility) is not bool
            or type(realized_exact) is not bool
        ):
            raise error_cls("slippage proof flags must be exact bool values")

        payload = {
            "schema": "autosport.betfair_standard_limit_price_bound",
            "schema_version": 2,
            "execution_plan_id": text(
                object.__getattribute__(value, "execution_plan_id"),
                "slippage execution_plan_id",
            ),
            "execution_plan_sha256": sha256(
                object.__getattribute__(value, "execution_plan_sha256"),
                "slippage execution_plan_sha256",
            ),
            "portfolio_plan_sha256": sha256(
                object.__getattribute__(value, "portfolio_plan_sha256"),
                "slippage portfolio_plan_sha256",
            ),
            "intent_id": text(
                object.__getattribute__(value, "intent_id"),
                "slippage intent_id",
            ),
            "intent_sha256": sha256(
                object.__getattribute__(value, "intent_sha256"),
                "slippage intent_sha256",
            ),
            "action_id": text(
                object.__getattribute__(value, "action_id"),
                "slippage action_id",
            ),
            "bookmaker_id": text(
                object.__getattribute__(value, "bookmaker_id"),
                "slippage bookmaker_id",
            ),
            "account_id": text(
                object.__getattribute__(value, "account_id"),
                "slippage account_id",
            ),
            "event_id": text(
                object.__getattribute__(value, "event_id"),
                "slippage event_id",
            ),
            "market_id": text(
                object.__getattribute__(value, "market_id"),
                "slippage market_id",
            ),
            "selection_id": text(
                object.__getattribute__(value, "selection_id"),
                "slippage selection_id",
            ),
            "side": text(
                object.__getattribute__(value, "side"),
                "slippage side",
            ),
            "requested_stake": str(requested_stake),
            "price_floor_odds": str(price_floor_odds),
            "quote_id": text(
                object.__getattribute__(value, "quote_id"),
                "slippage quote_id",
            ),
            "quote_observed_at": text(
                object.__getattribute__(value, "quote_observed_at"),
                "slippage quote_observed_at",
            ),
            "quote_expires_at": text(
                object.__getattribute__(value, "quote_expires_at"),
                "slippage quote_expires_at",
            ),
            "decision_at": text(
                object.__getattribute__(value, "decision_at"),
                "slippage decision_at",
            ),
            "instruction_sha256": sha256(
                object.__getattribute__(value, "instruction_sha256"),
                "slippage instruction_sha256",
            ),
            "provider_contract_id": text(
                object.__getattribute__(value, "provider_contract_id"),
                "slippage provider_contract_id",
            ),
            "provider_contract_ref": text(
                object.__getattribute__(value, "provider_contract_ref"),
                "slippage provider_contract_ref",
            ),
            "write_adapter_id": text(
                object.__getattribute__(value, "write_adapter_id"),
                "slippage write_adapter_id",
            ),
            "write_adapter_version": text(
                object.__getattribute__(value, "write_adapter_version"),
                "slippage write_adapter_version",
            ),
            "status": "PROVIDER_BOUND_ZERO_ADVERSE_PRICE_DETERIORATION",
            "matchme_applicability_proven": matchme_proven,
            "zero_adverse_price_deterioration": zero_adverse,
            "execution_feasibility_proven": feasibility,
            "realized_price_exact": realized_exact,
        }
        if json_dumps.__code__ is not json_dumps_code:
            raise error_cls("canonical JSON proof serializer authority changed")
        encoded = json_dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if json_dumps.__code__ is not json_dumps_code:
            raise error_cls("canonical JSON proof serializer authority changed")
        return sha256_constructor(encoded).hexdigest()

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

        risk_context = object.__getattribute__(intent, "risk_context")
        opportunity = object.__getattribute__(intent, "opportunity")
        if type(risk_context) is not risk_context_cls:
            raise error_cls(
                "intent risk_context must be the exact canonical ProposedTicketRiskContext type"
            )
        if type(opportunity) is not opportunity_cls:
            raise error_cls(
                "intent opportunity must be the exact canonical Opportunity type"
            )

        cutoff = instant(
            object.__getattribute__(risk_context, "proposal_ts"),
            "intent risk_context proposal_ts",
        )
        asserted_cutoff = instant(decision_at, "decision_at")
        if asserted_cutoff != cutoff:
            raise error_cls(
                "caller decision_at does not match canonical OpportunityIntent proposal_ts"
            )
        plan_cutoff = instant(
            object.__getattribute__(plan, "decision_ts"),
            "portfolio plan decision_ts",
        )
        if plan_cutoff != cutoff:
            raise error_cls(
                "portfolio plan decision_ts does not match OpportunityIntent proposal_ts"
            )

        if (
            intent_cls.intent_sha256.fget is not intent_sha_getter
            or intent_sha_getter.__code__ is not intent_sha_getter_code
        ):
            raise error_cls("canonical OpportunityIntent digest authority changed")
        intent_sha256 = sha256(
            intent_sha_getter(intent),
            "intent.intent_sha256",
        )
        if (
            intent_cls.intent_sha256.fget is not intent_sha_getter
            or intent_sha_getter.__code__ is not intent_sha_getter_code
        ):
            raise error_cls("canonical OpportunityIntent digest authority changed")
        intent_id = text(
            object.__getattribute__(intent, "intent_id"),
            "intent.intent_id",
        )
        plan_intent_sha256s = object.__getattribute__(plan, "intent_sha256s")
        plan_intent_ids = object.__getattribute__(plan, "intent_ids")
        if type(plan_intent_sha256s) is not tuple or type(plan_intent_ids) is not tuple:
            raise error_cls("canonical PortfolioPlan intent identity vectors must be tuples")
        canonical_plan_digests = tuple(
            sha256(candidate, "portfolio plan intent_sha256")
            for candidate in plan_intent_sha256s
        )
        canonical_plan_ids = tuple(
            text(candidate, "portfolio plan intent_id")
            for candidate in plan_intent_ids
        )
        if len(canonical_plan_digests) != len(canonical_plan_ids):
            raise error_cls(
                "canonical PortfolioPlan intent identity vectors must have matching cardinality"
            )
        matches = [
            index
            for index, candidate_digest in enumerate(canonical_plan_digests)
            if candidate_digest == intent_sha256
        ]
        if len(matches) != 1:
            raise error_cls(
                "canonical PortfolioPlan must bind the exact intent_sha256 exactly once"
            )
        index = matches[0]
        if canonical_plan_ids[index] != intent_id:
            raise error_cls("canonical PortfolioPlan intent_id/intent_sha256 binding mismatch")
        if (
            plan_cls.plan_sha256.fget is not plan_sha_getter
            or plan_sha_getter.__code__ is not plan_sha_getter_code
        ):
            raise error_cls("canonical PortfolioPlan digest authority changed")
        portfolio_plan_sha256 = sha256(
            plan_sha_getter(plan),
            "portfolio plan plan_sha256",
        )
        if (
            plan_cls.plan_sha256.fget is not plan_sha_getter
            or plan_sha_getter.__code__ is not plan_sha_getter_code
        ):
            raise error_cls("canonical PortfolioPlan digest authority changed")
        canonical_request_id = text(model_request_id, "model_request_id")

        if model_resolver.__code__ is not model_resolver_code:
            raise error_cls("canonical model-compute resolver authority changed")
        model_evidence = model_resolver(
            intent=intent,
            router_store=router_store,
            request_id=canonical_request_id,
            decision_at=cutoff,
        )
        if model_resolver.__code__ is not model_resolver_code:
            raise error_cls("canonical model-compute resolver authority changed")
        evidence_id = model_evidence_id(
            model_evidence,
            expected_request_id=canonical_request_id,
        )

        # The child resolver is not authority by return value. Re-read the exact
        # durable router state through closure-captured class methods and compare
        # canonical payload digests against the child evidence.
        if router_get_request.__code__ is not router_get_request_code:
            raise error_cls("canonical router request reader authority changed")
        durable_request = router_get_request(router_store, canonical_request_id)
        if router_get_request.__code__ is not router_get_request_code:
            raise error_cls("canonical router request reader authority changed")
        if type(durable_request) is not router_request_cls:
            raise error_cls("canonical router request read returned non-canonical type")
        if router_request_payload.__code__ is not router_request_payload_code:
            raise error_cls("canonical router request payload authority changed")
        durable_request_payload = router_request_payload(durable_request)
        if router_request_payload.__code__ is not router_request_payload_code:
            raise error_cls("canonical router request payload authority changed")
        if digest(durable_request_payload) != sha256(
            object.__getattribute__(model_evidence, "router_request_sha256"),
            "model router_request_sha256",
        ):
            raise error_cls("model-compute evidence request digest disagrees with durable router state")

        if router_get_decision.__code__ is not router_get_decision_code:
            raise error_cls("canonical router decision reader authority changed")
        durable_decision = router_get_decision(router_store, canonical_request_id)
        if router_get_decision.__code__ is not router_get_decision_code:
            raise error_cls("canonical router decision reader authority changed")
        if type(durable_decision) is not router_decision_cls:
            raise error_cls("canonical router decision read returned non-canonical type")
        if router_decision_payload.__code__ is not router_decision_payload_code:
            raise error_cls("canonical router decision payload authority changed")
        durable_decision_payload = router_decision_payload(durable_decision)
        if router_decision_payload.__code__ is not router_decision_payload_code:
            raise error_cls("canonical router decision payload authority changed")
        if digest(durable_decision_payload) != sha256(
            object.__getattribute__(model_evidence, "router_decision_sha256"),
            "model router_decision_sha256",
        ):
            raise error_cls("model-compute evidence decision digest disagrees with durable router state")
        if instant(
            object.__getattribute__(durable_decision, "decided_at"),
            "durable router decided_at",
        ) != instant(
            object.__getattribute__(model_evidence, "router_decided_at"),
            "model router_decided_at",
        ):
            raise error_cls("model-compute evidence decision time disagrees with durable router state")

        if sha256(
            object.__getattribute__(model_evidence, "intent_sha256"),
            "model intent_sha256",
        ) != intent_sha256:
            raise error_cls("model-compute evidence intent mismatch")
        opportunity_id = text(
            object.__getattribute__(opportunity, "opportunity_id"),
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

    resolve_code = resolve.__code__

    def resolve_with_betfair_standard_limit(
        *,
        intent: OpportunityIntent,
        plan: PortfolioPlan,
        router_store: ModelComputeRouterStore,
        model_request_id: str,
        decision_at: datetime,
        slippage_evidence: BetfairStandardLimitPriceBoundEvidence,
        ledger: RealExecutionLedger,
        issuance_store: SupervisedPlanIssuanceStore,
        runtime_profile: TrustedRuntimeCodeProfile,
        execution_plan_id: str,
        action_id: str,
    ) -> ProspectiveApplicableCostResolution:
        """Re-resolve aggregate truth with one exact product-verified slippage source."""

        if resolve.__code__ is not resolve_code:
            raise error_cls("canonical base applicable-cost resolver authority changed")
        if type(slippage_evidence) is not slippage_evidence_cls:
            raise error_cls(
                "slippage_evidence must be exact BetfairStandardLimitPriceBoundEvidence"
            )
        if type(ledger) is not ledger_cls:
            raise error_cls("ledger must be exact RealExecutionLedger")
        if type(issuance_store) is not issuance_store_cls:
            raise error_cls("issuance_store must be exact SupervisedPlanIssuanceStore")
        if type(runtime_profile) is not runtime_profile_cls:
            raise error_cls("runtime_profile must be exact TrustedRuntimeCodeProfile")
        text(execution_plan_id, "execution_plan_id")
        text(action_id, "action_id")
        if slippage_product_verifier.__code__ is not slippage_product_verifier_code:
            raise error_cls("canonical Betfair slippage verifier authority changed")

        try:
            verified = slippage_product_verifier(
                evidence=slippage_evidence,
                ledger=ledger,
                issuance_store=issuance_store,
                runtime_profile=runtime_profile,
                execution_plan_id=execution_plan_id,
                action_id=action_id,
            )
        except slippage_error_cls as exc:
            raise error_cls(
                "canonical Betfair slippage verification failed"
            ) from exc
        if slippage_product_verifier.__code__ is not slippage_product_verifier_code:
            raise error_cls("canonical Betfair slippage verifier authority changed")
        if type(verified) is not slippage_evidence_cls:
            raise error_cls("canonical Betfair slippage verifier returned non-canonical evidence")
        if text(
            object.__getattribute__(verified, "execution_plan_id"),
            "slippage execution_plan_id",
        ) != execution_plan_id:
            raise error_cls("Betfair slippage evidence execution plan mismatch")
        if text(
            object.__getattribute__(verified, "action_id"),
            "slippage action_id",
        ) != action_id:
            raise error_cls("Betfair slippage evidence action mismatch")
        if (
            object.__getattribute__(verified, "status")
            is not slippage_status_cls.PROVIDER_BOUND_ZERO_ADVERSE_PRICE_DETERIORATION
            or object.__getattribute__(verified, "matchme_applicability_proven") is not True
            or object.__getattribute__(verified, "zero_adverse_price_deterioration") is not True
            or object.__getattribute__(verified, "execution_feasibility_proven") is not False
            or object.__getattribute__(verified, "realized_price_exact") is not False
        ):
            raise error_cls(
                "Betfair slippage evidence does not prove the narrow zero-adverse-price contract"
            )

        if resolve.__code__ is not resolve_code:
            raise error_cls("canonical base applicable-cost resolver authority changed")
        base = resolve(
            intent=intent,
            plan=plan,
            router_store=router_store,
            model_request_id=model_request_id,
            decision_at=decision_at,
        )
        if resolve.__code__ is not resolve_code:
            raise error_cls("canonical base applicable-cost resolver authority changed")
        canonical_intent_sha = object.__getattribute__(base, "intent_sha256")
        canonical_plan_sha = object.__getattribute__(base, "portfolio_plan_sha256")
        canonical_opportunity_id = object.__getattribute__(base, "opportunity_id")
        cutoff = object.__getattribute__(base, "decision_at")
        if sha256(
            object.__getattribute__(verified, "intent_sha256"),
            "slippage intent_sha256",
        ) != canonical_intent_sha:
            raise error_cls("Betfair slippage evidence intent mismatch")
        if text(
            object.__getattribute__(verified, "intent_id"),
            "slippage intent_id",
        ) != text(object.__getattribute__(intent, "intent_id"), "intent.intent_id"):
            raise error_cls("Betfair slippage evidence intent id mismatch")
        if sha256(
            object.__getattribute__(verified, "portfolio_plan_sha256"),
            "slippage portfolio_plan_sha256",
        ) != canonical_plan_sha:
            raise error_cls("Betfair slippage evidence portfolio plan mismatch")
        if instant(
            object.__getattribute__(verified, "decision_at"),
            "slippage decision_at",
        ) != cutoff:
            raise error_cls("Betfair slippage evidence decision cutoff mismatch")
        text(canonical_opportunity_id, "opportunity_id")

        source_evidence_id = slippage_evidence_id(verified)
        replacement = issue_component(
            cost_class_cls.EXECUTION_SLIPPAGE,
            reason_cls.BETFAIR_STANDARD_LIMIT_ZERO_ADVERSE_PRICE,
            status=resolution_status_cls.KNOWN_ZERO,
            source_family=slippage_source_family,
            source_evidence_id=source_evidence_id,
            source_sha256=source_evidence_id,
        )
        components = tuple(
            replacement
            if object.__getattribute__(component, "cost_class")
            is cost_class_cls.EXECUTION_SLIPPAGE
            else component
            for component in object.__getattribute__(base, "components")
        )
        resolution = object.__new__(resolution_cls)
        object.__setattr__(resolution, "intent_sha256", canonical_intent_sha)
        object.__setattr__(resolution, "opportunity_id", canonical_opportunity_id)
        object.__setattr__(resolution, "portfolio_plan_sha256", canonical_plan_sha)
        object.__setattr__(resolution, "decision_at", cutoff)
        object.__setattr__(resolution, "components", components)
        object.__setattr__(resolution, "completeness", completeness_cls.INCOMPLETE)
        object.__setattr__(resolution, "total_subtractable_amount", None)
        object.__setattr__(resolution, "currency", None)
        validate_resolution(resolution)
        return resolution

    return resolve, resolve_with_betfair_standard_limit, validate_component, validate_resolution, (
        error_cls,
        component_cls,
        resolution_cls,
        intent_cls,
        plan_cls,
        router_store_cls,
    )


(
    resolve_prospective_applicable_costs,
    resolve_prospective_applicable_costs_with_betfair_standard_limit,
    _SEALED_COMPONENT_VALIDATOR,
    _SEALED_RESOLUTION_VALIDATOR,
    _SEALED_CANONICAL_TYPES,
) = _build_canonical_authority()
