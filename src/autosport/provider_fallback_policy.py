"""Fail-closed technical routing policy for degraded read-only providers.

This module grants neither provider-health, source-quality, economic nor execution
truth. It re-resolves technical capability from the canonical durable bookmaker
capability registry. A fallback route still requires a separate downstream source-
quality decision before observations may influence an economic decision. Account-
scoped reads may fail over only to another adapter/profile for the same venue/account;
cross-provider substitution is reserved for quote reads and still requires semantic
compatibility qualification downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from .bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerCapabilityRegistryError,
)
from .bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationEvidenceError,
)


class ProviderFallbackPolicyError(ValueError):
    pass


class ProviderOperationalState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderFallbackDisposition(StrEnum):
    USE_PRIMARY_READ = "USE_PRIMARY_READ"
    USE_FALLBACK_READ = "USE_FALLBACK_READ"
    ABSTAIN = "ABSTAIN"


_READ_ONLY_CAPABILITIES = frozenset(
    {
        BookmakerCapability.ACCOUNT_IDENTITY_READ,
        BookmakerCapability.BALANCE_READ,
        BookmakerCapability.LIMITS_READ,
        BookmakerCapability.PREMATCH_QUOTES_READ,
        BookmakerCapability.LIVE_QUOTES_READ,
        BookmakerCapability.BETSLIP_READ,
        BookmakerCapability.OPEN_POSITIONS_READ,
        BookmakerCapability.SETTLED_POSITIONS_READ,
    }
)

_ACCOUNT_SCOPED_READ_CAPABILITIES = frozenset(
    {
        BookmakerCapability.ACCOUNT_IDENTITY_READ,
        BookmakerCapability.BALANCE_READ,
        BookmakerCapability.LIMITS_READ,
        BookmakerCapability.BETSLIP_READ,
        BookmakerCapability.OPEN_POSITIONS_READ,
        BookmakerCapability.SETTLED_POSITIONS_READ,
    }
)

_CROSS_PROVIDER_QUOTE_READ_CAPABILITIES = frozenset(
    {
        BookmakerCapability.PREMATCH_QUOTES_READ,
        BookmakerCapability.LIVE_QUOTES_READ,
    }
)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProviderFallbackPolicyError(f"{field} must be a non-empty canonical string")
    return value


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ProviderFallbackPolicyError(f"{field} must be lowercase SHA-256")
    return text


def _instant(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderFallbackPolicyError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderFallbackPolicyError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _digest(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderRouteIdentity:
    venue_id: str
    account_id: str
    adapter_id: str
    profile_id: str

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _sha(self.profile_id, "profile_id")

    def resolve_latest(self, registry: BookmakerCapabilityRegistry) -> BookmakerCapabilityProfile:
        if not isinstance(registry, BookmakerCapabilityRegistry):
            raise ProviderFallbackPolicyError("registry must be BookmakerCapabilityRegistry")
        try:
            profile = registry.latest_profile(self.venue_id, self.account_id, self.adapter_id)
        except BookmakerCapabilityRegistryError as exc:
            raise ProviderFallbackPolicyError("cannot resolve canonical capability profile") from exc
        if profile is None:
            raise ProviderFallbackPolicyError("route has no durable capability profile")
        if profile.profile_id != self.profile_id:
            raise ProviderFallbackPolicyError("route profile_id is not the latest durable profile")
        return profile


@dataclass(frozen=True, slots=True)
class ProviderOperationalObservation:
    """Non-authoritative operational state bound to an exact technical profile."""

    route: ProviderRouteIdentity
    state: ProviderOperationalState
    observed_at: str
    source_ref: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        if type(self.route) is not ProviderRouteIdentity:
            raise ProviderFallbackPolicyError("route must be ProviderRouteIdentity")
        if type(self.state) is not ProviderOperationalState:
            raise ProviderFallbackPolicyError("state must be ProviderOperationalState")
        _instant(self.observed_at, "observed_at")
        _text(self.source_ref, "source_ref")
        _sha(self.source_payload_sha256, "source_payload_sha256")

    @property
    def observation_id(self) -> str:
        return _digest(
            {
                "venue_id": self.route.venue_id,
                "account_id": self.route.account_id,
                "adapter_id": self.route.adapter_id,
                "profile_id": self.route.profile_id,
                "state": self.state.value,
                "observed_at": self.observed_at,
                "source_ref": self.source_ref,
                "source_payload_sha256": self.source_payload_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class ProviderFallbackDecision:
    disposition: ProviderFallbackDisposition
    required_capability: BookmakerCapability
    primary_profile_id: str
    primary_observation_id: str
    selected_profile_id: str | None
    selected_integration_evidence_id: str | None
    fallback_profile_id: str | None
    fallback_observation_id: str | None
    decided_at: str
    reason: str

    @property
    def source_quality_authority(self) -> bool:
        return False

    @property
    def execution_authority(self) -> bool:
        return False

    @property
    def downstream_source_quality_required(self) -> bool:
        return True

    @property
    def downstream_semantic_compatibility_required(self) -> bool:
        return (
            self.disposition is ProviderFallbackDisposition.USE_FALLBACK_READ
            and self.required_capability in _CROSS_PROVIDER_QUOTE_READ_CAPABILITIES
        )

    @property
    def decision_id(self) -> str:
        return _digest(
            {
                "disposition": self.disposition.value,
                "required_capability": self.required_capability.value,
                "primary_profile_id": self.primary_profile_id,
                "primary_observation_id": self.primary_observation_id,
                "selected_profile_id": self.selected_profile_id,
                "selected_integration_evidence_id": self.selected_integration_evidence_id,
                "fallback_profile_id": self.fallback_profile_id,
                "fallback_observation_id": self.fallback_observation_id,
                "decided_at": self.decided_at,
                "reason": self.reason,
            }
        )


def _resolve(
    *,
    registry: BookmakerCapabilityRegistry,
    route: ProviderRouteIdentity,
    integration: BookmakerIntegrationEvidence,
    operational: ProviderOperationalObservation,
    decided_at: datetime,
    field: str,
) -> BookmakerCapabilityProfile:
    if type(route) is not ProviderRouteIdentity:
        raise ProviderFallbackPolicyError(f"{field} route has invalid type")
    if type(operational) is not ProviderOperationalObservation or operational.route != route:
        raise ProviderFallbackPolicyError(f"{field} operational evidence binds another route")
    if type(integration) is not BookmakerIntegrationEvidence:
        raise ProviderFallbackPolicyError(f"{field} integration has invalid type")
    profile = route.resolve_latest(registry)
    try:
        integration.verify_profile(profile)
    except BookmakerIntegrationEvidenceError as exc:
        raise ProviderFallbackPolicyError(f"{field} integration/profile mismatch") from exc
    profile_at = _instant(profile.observed_at, f"{field}.profile.observed_at")
    operational_at = _instant(operational.observed_at, f"{field}.operational.observed_at")
    integration_at = _instant(integration.observed_at, f"{field}.integration.observed_at")
    if operational_at < profile_at:
        raise ProviderFallbackPolicyError(f"{field} operational evidence predates profile")
    if max(profile_at, operational_at, integration_at) > decided_at:
        raise ProviderFallbackPolicyError(f"{field} route evidence is future at decided_at")
    return profile


def _decision(
    *,
    disposition: ProviderFallbackDisposition,
    capability: BookmakerCapability,
    primary: BookmakerCapabilityProfile,
    primary_observation: ProviderOperationalObservation,
    decided_at: str,
    reason: str,
    selected: BookmakerCapabilityProfile | None = None,
    selected_integration: BookmakerIntegrationEvidence | None = None,
    fallback: BookmakerCapabilityProfile | None = None,
    fallback_observation: ProviderOperationalObservation | None = None,
) -> ProviderFallbackDecision:
    return ProviderFallbackDecision(
        disposition=disposition,
        required_capability=capability,
        primary_profile_id=primary.profile_id,
        primary_observation_id=primary_observation.observation_id,
        selected_profile_id=selected.profile_id if selected else None,
        selected_integration_evidence_id=(
            selected_integration.evidence_id if selected_integration else None
        ),
        fallback_profile_id=fallback.profile_id if fallback else None,
        fallback_observation_id=(fallback_observation.observation_id if fallback_observation else None),
        decided_at=decided_at,
        reason=reason,
    )


def resolve_readonly_provider_route(
    *,
    registry: BookmakerCapabilityRegistry,
    required_capability: BookmakerCapability,
    primary_route: ProviderRouteIdentity,
    primary_integration: BookmakerIntegrationEvidence,
    primary_operational: ProviderOperationalObservation,
    decided_at: str,
    fallback_route: ProviderRouteIdentity | None = None,
    fallback_integration: BookmakerIntegrationEvidence | None = None,
    fallback_operational: ProviderOperationalObservation | None = None,
) -> ProviderFallbackDecision:
    """Choose only a technically eligible read route; never grant downstream trust."""

    if type(required_capability) is not BookmakerCapability:
        raise ProviderFallbackPolicyError("required_capability must be BookmakerCapability")
    if required_capability not in _READ_ONLY_CAPABILITIES:
        raise ProviderFallbackPolicyError("fallback policy is restricted to read-only capabilities")
    decision_at = _instant(decided_at, "decided_at")
    primary = _resolve(
        registry=registry,
        route=primary_route,
        integration=primary_integration,
        operational=primary_operational,
        decided_at=decision_at,
        field="primary",
    )
    primary_capability = primary.state_of(required_capability)

    supplied = (
        fallback_route is not None,
        fallback_integration is not None,
        fallback_operational is not None,
    )
    if any(supplied) and not all(supplied):
        raise ProviderFallbackPolicyError("fallback evidence must be supplied together")

    if primary_operational.state is ProviderOperationalState.HEALTHY:
        if primary_capability is BookmakerCapabilityState.SUPPORTED:
            return _decision(
                disposition=ProviderFallbackDisposition.USE_PRIMARY_READ,
                capability=required_capability,
                primary=primary,
                primary_observation=primary_operational,
                selected=primary,
                selected_integration=primary_integration,
                decided_at=decided_at,
                reason="PRIMARY_HEALTHY_SUPPORTED",
            )
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary=primary,
            primary_observation=primary_operational,
            decided_at=decided_at,
            reason=f"PRIMARY_HEALTHY_CAPABILITY_{primary_capability.value.upper()}",
        )

    if not all(supplied):
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary=primary,
            primary_observation=primary_operational,
            decided_at=decided_at,
            reason=f"PRIMARY_{primary_operational.state.value}_NO_FALLBACK",
        )

    assert fallback_route is not None
    assert fallback_integration is not None
    assert fallback_operational is not None
    if fallback_route.profile_id == primary_route.profile_id:
        raise ProviderFallbackPolicyError("fallback must not reuse exact primary profile")
    fallback = _resolve(
        registry=registry,
        route=fallback_route,
        integration=fallback_integration,
        operational=fallback_operational,
        decided_at=decision_at,
        field="fallback",
    )
    if (
        required_capability in _ACCOUNT_SCOPED_READ_CAPABILITIES
        and (fallback.venue_id, fallback.account_id)
        != (primary.venue_id, primary.account_id)
    ):
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary=primary,
            primary_observation=primary_operational,
            fallback=fallback,
            fallback_observation=fallback_operational,
            decided_at=decided_at,
            reason="ACCOUNT_SCOPED_FALLBACK_IDENTITY_MISMATCH",
        )
    if fallback_operational.state is not ProviderOperationalState.HEALTHY:
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary=primary,
            primary_observation=primary_operational,
            fallback=fallback,
            fallback_observation=fallback_operational,
            decided_at=decided_at,
            reason=f"FALLBACK_{fallback_operational.state.value}",
        )
    fallback_capability = fallback.state_of(required_capability)
    if fallback_capability is not BookmakerCapabilityState.SUPPORTED:
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary=primary,
            primary_observation=primary_operational,
            fallback=fallback,
            fallback_observation=fallback_operational,
            decided_at=decided_at,
            reason=f"FALLBACK_CAPABILITY_{fallback_capability.value.upper()}",
        )
    return _decision(
        disposition=ProviderFallbackDisposition.USE_FALLBACK_READ,
        capability=required_capability,
        primary=primary,
        primary_observation=primary_operational,
        selected=fallback,
        selected_integration=fallback_integration,
        fallback=fallback,
        fallback_observation=fallback_operational,
        decided_at=decided_at,
        reason=f"PRIMARY_{primary_operational.state.value}_FALLBACK_HEALTHY_SUPPORTED",
    )