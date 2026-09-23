"""Fail-closed composition boundary for exact provider execution scope.

This module deliberately does not create provider-write entitlement. It composes already
existing canonical evidence into a deterministic pre-admission report and keeps every
authority-bearing gap explicit. Until a provider-specific, product-issued write-entitlement
resolver is integrated, execution_admitted is unconditionally false.

The report is diagnostic/staging evidence only. It is not an execution token, capability
profile, settlement authority, risk approval, STOP authority, or provider request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from .bookmaker_capability_registry import (
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)
from .bookmaker_integration_boundary import BookmakerIntegrationEvidence
from .supervised_execution import BoundSupervisedExecutionPlan


class ExecutionScopeAdmissionError(ValueError):
    """Raised when pre-admission inputs conflict or are not canonical."""


class ExecutionDataMode(str, Enum):
    PREMATCH = "PREMATCH"
    LIVE = "LIVE"


class ExecutionAdmissionBlocker(str, Enum):
    ACTION_NOT_DOCUMENTED = "ACTION_NOT_DOCUMENTED"
    GOVERNANCE_AUTOMATION_NOT_PERMITTED = "GOVERNANCE_AUTOMATION_NOT_PERMITTED"
    PROVIDER_PRODUCT_DOMAIN_UNPROVEN = "PROVIDER_PRODUCT_DOMAIN_UNPROVEN"
    AUTHENTICATED_CONTEXT_UNPROVEN = "AUTHENTICATED_CONTEXT_UNPROVEN"
    WRITE_ENTITLEMENT_UNPROVEN = "WRITE_ENTITLEMENT_UNPROVEN"
    APP_KEY_PURPOSE_UNPROVEN = "APP_KEY_PURPOSE_UNPROVEN"
    CURRENT_SCOPE_CAPABILITY_UNPROVEN = "CURRENT_SCOPE_CAPABILITY_UNPROVEN"
    MARKET_DATA_MODE_UNPROVEN = "MARKET_DATA_MODE_UNPROVEN"
    SPORT_MARKET_SEMANTICS_UNPROVEN = "SPORT_MARKET_SEMANTICS_UNPROVEN"
    SETTLEMENT_RULE_UNPROVEN = "SETTLEMENT_RULE_UNPROVEN"
    ECONOMIC_RISK_GATES_UNPROVEN = "ECONOMIC_RISK_GATES_UNPROVEN"
    DISPATCH_REQUEST_UNBOUND = "DISPATCH_REQUEST_UNBOUND"
    PRODUCT_SUPERVISED_ACTION_UNPROVEN = "PRODUCT_SUPERVISED_ACTION_UNPROVEN"\n    SINGLE_USE_RESTART_UNPROVEN = "SINGLE_USE_RESTART_UNPROVEN"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ExecutionScopeAdmissionError(
            f"{field} must be non-empty canonical text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ExecutionScopeAdmissionError(
            f"{field} must be UTF-8 encodable"
        ) from exc
    return value


def _timestamp(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionScopeAdmissionError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionScopeAdmissionError(
            f"{field} must be timezone-aware"
        )
    return parsed


def _canonical_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExecutionScopeAdmissionError(
            "execution scope is not canonical JSON"
        ) from exc
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionScopeDescriptor:
    """Immutable caller request shape; never positive authority by itself."""

    action_id: str
    provider_product_domain: str
    environment: str
    jurisdiction: str
    sport: str
    event_id: str
    market_id: str
    selection_id: str
    market_semantics_id: str
    settlement_rule_id: str
    data_mode: ExecutionDataMode
    action_kind: str
    order_type: str
    side: str
    currency: str
    strategy_id: str
    purpose: str
    evidence_cutoff: str
    attempt_id: str

    def __post_init__(self) -> None:
        for field in (
            "action_id",
            "provider_product_domain",
            "environment",
            "jurisdiction",
            "sport",
            "event_id",
            "market_id",
            "selection_id",
            "market_semantics_id",
            "settlement_rule_id",
            "action_kind",
            "order_type",
            "side",
            "currency",
            "strategy_id",
            "purpose",
            "attempt_id",
        ):
            _text(getattr(self, field), field)
        if type(self.data_mode) is not ExecutionDataMode:
            raise ExecutionScopeAdmissionError(
                "data_mode must be ExecutionDataMode"
            )
        _timestamp(self.evidence_cutoff, "evidence_cutoff")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "provider_product_domain": self.provider_product_domain,
            "environment": self.environment,
            "jurisdiction": self.jurisdiction,
            "sport": self.sport,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "market_semantics_id": self.market_semantics_id,
            "settlement_rule_id": self.settlement_rule_id,
            "data_mode": self.data_mode.value,
            "action_kind": self.action_kind,
            "order_type": self.order_type,
            "side": self.side,
            "currency": self.currency,
            "strategy_id": self.strategy_id,
            "purpose": self.purpose,
            "evidence_cutoff": self.evidence_cutoff,
            "attempt_id": self.attempt_id,
        }

    @property
    def descriptor_sha256(self) -> str:
        return _canonical_digest(self.to_canonical_dict())


@dataclass(frozen=True, slots=True)
class ExecutionScopePreAdmission:
    """Factorized, non-authorizing report over current canonical evidence."""

    scope: ExecutionScopeDescriptor
    scope_sha256: str
    profile_id: str
    governance_evidence_id: str
    integration_evidence_id: str
    supervised_plan_id: str
    provider_action_documented: bool
    governance_automation_permitted: bool
    integration_channel_bound: bool
    product_supervised_action_issued: bool
    blocking_reasons: tuple[ExecutionAdmissionBlocker, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not ExecutionScopeDescriptor:
            raise ExecutionScopeAdmissionError(
                "scope must be exact ExecutionScopeDescriptor"
            )
        for field in (
            "scope_sha256",
            "profile_id",
            "governance_evidence_id",
            "integration_evidence_id",
            "supervised_plan_id",
        ):
            _text(getattr(self, field), field)
        for field in (
            "provider_action_documented",
            "governance_automation_permitted",
            "integration_channel_bound",
            "supervised_action_binding_structurally_valid",
        ):
            if type(getattr(self, field)) is not bool:
                raise ExecutionScopeAdmissionError(f"{field} must be bool")
        if (
            type(self.blocking_reasons) is not tuple
            or any(
                type(item) is not ExecutionAdmissionBlocker
                for item in self.blocking_reasons
            )
            or len(set(self.blocking_reasons)) != len(self.blocking_reasons)
        ):
            raise ExecutionScopeAdmissionError(
                "blocking_reasons must be unique ExecutionAdmissionBlocker values"
            )

    @property
    def provider_write_entitlement_proven(self) -> bool:
        return False

    @property
    def authenticated_context_proven(self) -> bool:
        return False

    @property
    def current_scope_capability_proven(self) -> bool:
        return False

    @property
    def market_data_mode_suitable(self) -> bool:
        return False

    @property
    def sport_market_semantics_proven(self) -> bool:
        return False

    @property
    def settlement_rule_supported(self) -> bool:
        return False

    @property
    def economic_risk_gates_passed(self) -> bool:
        return False

    @property
    def dispatch_request_exactly_bound(self) -> bool:
        return False

    @property
    def execution_admitted(self) -> bool:
        """Positive execution admission is intentionally impossible in schema v1."""

        return False

    @property
    def assessment_sha256(self) -> str:
        return _canonical_digest(
            {
                "schema": "autosport.execution_scope_pre_admission",
                "schema_version": 1,
                "scope": self.scope.to_canonical_dict(),
                "scope_sha256": self.scope_sha256,
                "profile_id": self.profile_id,
                "governance_evidence_id": self.governance_evidence_id,
                "integration_evidence_id": self.integration_evidence_id,
                "supervised_plan_id": self.supervised_plan_id,
                "provider_action_documented": self.provider_action_documented,
                "governance_automation_permitted": self.governance_automation_permitted,
                "integration_channel_bound": self.integration_channel_bound,
                "product_supervised_action_issued": (\n                    self.product_supervised_action_issued\n                ),
                "provider_write_entitlement_proven": (\n                    self.provider_write_entitlement_proven\n                ),
                "authenticated_context_proven": self.authenticated_context_proven,
                "current_scope_capability_proven": self.current_scope_capability_proven,
                "market_data_mode_suitable": self.market_data_mode_suitable,
                "sport_market_semantics_proven": self.sport_market_semantics_proven,
                "settlement_rule_supported": self.settlement_rule_supported,
                "economic_risk_gates_passed": self.economic_risk_gates_passed,
                "dispatch_request_exactly_bound": self.dispatch_request_exactly_bound,
                "execution_admitted": self.execution_admitted,
                "blocking_reasons": [
                    item.value for item in self.blocking_reasons
                ],
            }
        )


def assess_execution_scope_pre_admission(
    scope: ExecutionScopeDescriptor,
    *,
    profile: BookmakerCapabilityProfile,
    governance: BookmakerGovernanceEvidence,
    integration: BookmakerIntegrationEvidence,
    bound_plan: BoundSupervisedExecutionPlan,
) -> ExecutionScopePreAdmission:
    """Compose current exact evidence without widening provider-write authority."""

    if type(scope) is not ExecutionScopeDescriptor:
        raise ExecutionScopeAdmissionError(
            "scope must be exact ExecutionScopeDescriptor"
        )
    if type(profile) is not BookmakerCapabilityProfile:
        raise ExecutionScopeAdmissionError(
            "profile must be exact BookmakerCapabilityProfile"
        )
    if type(governance) is not BookmakerGovernanceEvidence:
        raise ExecutionScopeAdmissionError(
            "governance must be exact BookmakerGovernanceEvidence"
        )
    if type(integration) is not BookmakerIntegrationEvidence:
        raise ExecutionScopeAdmissionError(
            "integration must be exact BookmakerIntegrationEvidence"
        )
    if type(bound_plan) is not BoundSupervisedExecutionPlan:
        raise ExecutionScopeAdmissionError(
            "bound_plan must be exact BoundSupervisedExecutionPlan"
        )

    bound_plan.verify_binding()
    integration.verify_profile(profile)
    action = bound_plan.action_for(scope.action_id)
    profile_binding = bound_plan.profile_for(
        action.bookmaker_id,
        action.account_id,
    )

    if (
        profile.venue_id,
        profile.account_id,
        profile.adapter_id,
        profile.adapter_version,
        profile.profile_version,
        profile.profile_id,
    ) != (
        action.bookmaker_id,
        action.account_id,
        profile_binding.adapter_id,
        profile_binding.adapter_version,
        profile_binding.profile_version,
        profile_binding.profile_sha256,
    ):
        raise ExecutionScopeAdmissionError(
            "capability profile does not match supervised action binding"
        )

    if (
        scope.event_id,
        scope.market_id,
        scope.selection_id,
        scope.side,
    ) != (
        action.event_id,
        action.market_id,
        action.selection_id,
        action.side,
    ):
        raise ExecutionScopeAdmissionError(
            "execution scope does not match supervised action identity"
        )

    if (
        governance.venue_id != profile.venue_id
        or governance.account_id != profile.account_id
        or governance.jurisdiction != scope.jurisdiction
    ):
        raise ExecutionScopeAdmissionError(
            "governance evidence does not match execution account/jurisdiction"
        )

    cutoff = _timestamp(scope.evidence_cutoff, "evidence_cutoff")
    for value, field in (
        (profile.observed_at, "profile.observed_at"),
        (governance.observed_at, "governance.observed_at"),
        (integration.observed_at, "integration.observed_at"),
        (
            bound_plan.execution_plan.created_at,
            "execution_plan.created_at",
        ),
    ):
        if _timestamp(value, field) > cutoff:
            raise ExecutionScopeAdmissionError(
                f"{field} is future evidence at execution scope cutoff"
            )

    action_state = profile.state_of(BookmakerCapability.PLACE_BET)
    provider_action_documented = (
        action_state is BookmakerCapabilityState.SUPPORTED
    )
    governance_permitted = (
        governance.automation_permission
        is GovernancePermissionState.PERMITTED
    )

    blockers: list[ExecutionAdmissionBlocker] = []
    if not provider_action_documented:
        blockers.append(ExecutionAdmissionBlocker.ACTION_NOT_DOCUMENTED)
    if not governance_permitted:
        blockers.append(
            ExecutionAdmissionBlocker.GOVERNANCE_AUTOMATION_NOT_PERMITTED
        )
    blockers.extend(
        (
            ExecutionAdmissionBlocker.PROVIDER_PRODUCT_DOMAIN_UNPROVEN,
            ExecutionAdmissionBlocker.AUTHENTICATED_CONTEXT_UNPROVEN,
            ExecutionAdmissionBlocker.WRITE_ENTITLEMENT_UNPROVEN,
            ExecutionAdmissionBlocker.APP_KEY_PURPOSE_UNPROVEN,
            ExecutionAdmissionBlocker.CURRENT_SCOPE_CAPABILITY_UNPROVEN,
            ExecutionAdmissionBlocker.MARKET_DATA_MODE_UNPROVEN,
            ExecutionAdmissionBlocker.SPORT_MARKET_SEMANTICS_UNPROVEN,
            ExecutionAdmissionBlocker.SETTLEMENT_RULE_UNPROVEN,
            ExecutionAdmissionBlocker.ECONOMIC_RISK_GATES_UNPROVEN,
            ExecutionAdmissionBlocker.DISPATCH_REQUEST_UNBOUND,
            ExecutionAdmissionBlocker.SINGLE_USE_RESTART_UNPROVEN,
        )
    )

    scope_sha256 = _canonical_digest(
        {
            "schema": "autosport.execution_scope_pre_admission.scope",
            "schema_version": 1,
            "descriptor": scope.to_canonical_dict(),
            "profile_id": profile.profile_id,
            "governance_evidence_id": governance.evidence_id,
            "integration_evidence_id": integration.evidence_id,
            "supervised_plan_id": bound_plan.execution_plan.plan_id,
        }
    )

    return ExecutionScopePreAdmission(
        scope=scope,
        scope_sha256=scope_sha256,
        profile_id=profile.profile_id,
        governance_evidence_id=governance.evidence_id,
        integration_evidence_id=integration.evidence_id,
        supervised_plan_id=bound_plan.execution_plan.plan_id,
        provider_action_documented=provider_action_documented,
        governance_automation_permitted=governance_permitted,
        integration_channel_bound=True,
        product_supervised_action_issued=True,
        blocking_reasons=tuple(blockers),
    )
