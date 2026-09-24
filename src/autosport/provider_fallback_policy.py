"""Fail-closed technical eligibility policy for degraded read-only providers.

Provider health is re-resolved from the existing durable SourceHealthStore as of the
decision instant; this module creates no second health registry. Technical bookmaker
capability is re-resolved from BookmakerCapabilityRegistry and exact integration
evidence must bind that profile.

The output is eligibility evidence only. It grants no source-quality, semantic,
economic, settlement or execution authority. A consumer must separately bind source
identity/quality and, for cross-provider quote reads, semantic market compatibility.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import math

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
from .ingestion_health import IngestionPolicy, SourceHealthState, SourceHealthStore


class ProviderFallbackPolicyError(ValueError):
    pass


class ProviderOperationalState(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"


class ProviderFallbackDisposition(StrEnum):
    PRIMARY_TECHNICALLY_ELIGIBLE = "PRIMARY_TECHNICALLY_ELIGIBLE"
    FALLBACK_TECHNICALLY_ELIGIBLE = "FALLBACK_TECHNICALLY_ELIGIBLE"
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


def _source_id(value: object, field: str) -> str:
    text = _text(value, field)
    if "|" in text:
        raise ProviderFallbackPolicyError(f"{field} contains reserved identity delimiter")
    return text


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
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderRouteIdentity:
    """Caller-declared mapping that must still pass durable downstream identity binding."""

    source_id: str
    venue_id: str
    account_id: str
    adapter_id: str
    profile_id: str

    def __post_init__(self) -> None:
        _source_id(self.source_id, "source_id")
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _sha(self.profile_id, "profile_id")

    def resolve_latest(
        self,
        registry: BookmakerCapabilityRegistry,
    ) -> BookmakerCapabilityProfile:
        if not isinstance(registry, BookmakerCapabilityRegistry):
            raise ProviderFallbackPolicyError(
                "registry must be BookmakerCapabilityRegistry"
            )
        try:
            profile = registry.latest_profile(
                self.venue_id,
                self.account_id,
                self.adapter_id,
            )
        except BookmakerCapabilityRegistryError as exc:
            raise ProviderFallbackPolicyError(
                "cannot resolve canonical capability profile"
            ) from exc
        if profile is None:
            raise ProviderFallbackPolicyError(
                "route has no durable capability profile"
            )
        if profile.profile_id != self.profile_id:
            raise ProviderFallbackPolicyError(
                "route profile_id is not the latest durable profile"
            )
        return profile


@dataclass(frozen=True, slots=True)
class _ResolvedHealth:
    state: ProviderOperationalState
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class ProviderFallbackDecision:
    disposition: ProviderFallbackDisposition
    required_capability: BookmakerCapability
    primary_source_id: str
    primary_profile_id: str
    primary_health_state: ProviderOperationalState
    primary_health_snapshot_id: str
    selected_source_id: str | None
    selected_profile_id: str | None
    selected_integration_evidence_id: str | None
    fallback_source_id: str | None
    fallback_profile_id: str | None
    fallback_health_state: ProviderOperationalState | None
    fallback_health_snapshot_id: str | None
    health_stale_after_seconds: float
    decided_at: str
    reason: str

    def __post_init__(self) -> None:
        if type(self.disposition) is not ProviderFallbackDisposition:
            raise ProviderFallbackPolicyError("invalid fallback disposition")
        if type(self.required_capability) is not BookmakerCapability:
            raise ProviderFallbackPolicyError("invalid required capability")
        _source_id(self.primary_source_id, "primary_source_id")
        _sha(self.primary_profile_id, "primary_profile_id")
        if type(self.primary_health_state) is not ProviderOperationalState:
            raise ProviderFallbackPolicyError("invalid primary health state")
        _sha(self.primary_health_snapshot_id, "primary_health_snapshot_id")
        for name in ("selected_source_id", "fallback_source_id"):
            value = getattr(self, name)
            if value is not None:
                _source_id(value, name)
        for name in (
            "selected_profile_id",
            "selected_integration_evidence_id",
            "fallback_profile_id",
            "fallback_health_snapshot_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _sha(value, name)
        if (
            self.fallback_health_state is not None
            and type(self.fallback_health_state) is not ProviderOperationalState
        ):
            raise ProviderFallbackPolicyError("invalid fallback health state")
        if (
            isinstance(self.health_stale_after_seconds, bool)
            or not isinstance(self.health_stale_after_seconds, (int, float))
            or not math.isfinite(self.health_stale_after_seconds)
            or self.health_stale_after_seconds < 0
        ):
            raise ProviderFallbackPolicyError(
                "health_stale_after_seconds must be finite and non-negative"
            )
        _instant(self.decided_at, "decided_at")
        _text(self.reason, "reason")

        selected = self.disposition is not ProviderFallbackDisposition.ABSTAIN
        if selected != (self.selected_source_id is not None):
            raise ProviderFallbackPolicyError(
                "selected_source_id presence must match disposition"
            )
        if selected != (self.selected_profile_id is not None):
            raise ProviderFallbackPolicyError(
                "selected_profile_id presence must match disposition"
            )
        if selected != (self.selected_integration_evidence_id is not None):
            raise ProviderFallbackPolicyError(
                "selected integration evidence presence must match disposition"
            )
        if (
            self.disposition
            is ProviderFallbackDisposition.FALLBACK_TECHNICALLY_ELIGIBLE
            and (
                self.selected_source_id != self.fallback_source_id
                or self.selected_profile_id != self.fallback_profile_id
                or self.fallback_health_snapshot_id is None
            )
        ):
            raise ProviderFallbackPolicyError(
                "fallback eligibility must select exact fallback evidence"
            )

    @property
    def source_quality_authority(self) -> bool:
        return False

    @property
    def source_identity_authority(self) -> bool:
        return False

    @property
    def execution_authority(self) -> bool:
        return False

    @property
    def downstream_source_quality_required(self) -> bool:
        return self.disposition is not ProviderFallbackDisposition.ABSTAIN

    @property
    def downstream_source_identity_binding_required(self) -> bool:
        return self.disposition is not ProviderFallbackDisposition.ABSTAIN

    @property
    def downstream_semantic_compatibility_required(self) -> bool:
        return (
            self.disposition
            is ProviderFallbackDisposition.FALLBACK_TECHNICALLY_ELIGIBLE
            and self.required_capability
            in _CROSS_PROVIDER_QUOTE_READ_CAPABILITIES
            and self.selected_source_id != self.primary_source_id
        )

    @property
    def decision_id(self) -> str:
        return _digest(
            {
                "disposition": self.disposition.value,
                "required_capability": self.required_capability.value,
                "primary_source_id": self.primary_source_id,
                "primary_profile_id": self.primary_profile_id,
                "primary_health_state": self.primary_health_state.value,
                "primary_health_snapshot_id": self.primary_health_snapshot_id,
                "selected_source_id": self.selected_source_id,
                "selected_profile_id": self.selected_profile_id,
                "selected_integration_evidence_id": self.selected_integration_evidence_id,
                "fallback_source_id": self.fallback_source_id,
                "fallback_profile_id": self.fallback_profile_id,
                "fallback_health_state": (
                    self.fallback_health_state.value
                    if self.fallback_health_state is not None
                    else None
                ),
                "fallback_health_snapshot_id": self.fallback_health_snapshot_id,
                "health_stale_after_seconds": float(self.health_stale_after_seconds),
                "decided_at": self.decided_at,
                "reason": self.reason,
            }
        )


def _health_snapshot_id(
    state: SourceHealthState,
    *,
    as_of: datetime,
    resolved_state: ProviderOperationalState,
) -> str:
    payload = asdict(state)
    payload["quality_flags"] = list(state.quality_flags)
    return _digest(
        {
            "as_of": as_of.isoformat(),
            "resolved_state": resolved_state.value,
            "state": payload,
        }
    )


def _resolve_health(
    *,
    health_store: SourceHealthStore,
    source_id: str,
    decided_at: datetime,
    policy: IngestionPolicy,
) -> _ResolvedHealth:
    if not isinstance(health_store, SourceHealthStore):
        raise ProviderFallbackPolicyError(
            "health_store must be SourceHealthStore"
        )
    if not isinstance(policy, IngestionPolicy):
        raise ProviderFallbackPolicyError("policy must be IngestionPolicy")
    try:
        state = health_store.get_as_of(source_id, as_of=decided_at)
    except (OSError, TypeError, ValueError) as exc:
        raise ProviderFallbackPolicyError(
            "cannot resolve durable source health"
        ) from exc
    if state.source_id != source_id:
        raise ProviderFallbackPolicyError("source health identity mismatch")

    if state.status == "unknown":
        resolved = ProviderOperationalState.UNKNOWN
    elif state.status == "failed":
        resolved = ProviderOperationalState.UNAVAILABLE
    elif state.status in {"healthy", "degraded"}:
        if state.last_success_at is None:
            raise ProviderFallbackPolicyError(
                "successful source health lacks last_success_at"
            )
        success_at = _instant(state.last_success_at, "source_health.last_success_at")
        age_seconds = (decided_at - success_at).total_seconds()
        if age_seconds < 0:
            raise ProviderFallbackPolicyError(
                "source health success is future at decided_at"
            )
        if age_seconds > policy.stale_after_seconds:
            resolved = ProviderOperationalState.STALE
        elif state.status == "healthy":
            resolved = ProviderOperationalState.HEALTHY
        else:
            resolved = ProviderOperationalState.DEGRADED
    else:
        raise ProviderFallbackPolicyError("unsupported durable source health status")

    return _ResolvedHealth(
        state=resolved,
        snapshot_id=_health_snapshot_id(
            state,
            as_of=decided_at,
            resolved_state=resolved,
        ),
    )


def _resolve_profile(
    *,
    registry: BookmakerCapabilityRegistry,
    route: ProviderRouteIdentity,
    integration: BookmakerIntegrationEvidence,
    decided_at: datetime,
    field: str,
) -> BookmakerCapabilityProfile:
    if type(route) is not ProviderRouteIdentity:
        raise ProviderFallbackPolicyError(f"{field} route has invalid type")
    if type(integration) is not BookmakerIntegrationEvidence:
        raise ProviderFallbackPolicyError(f"{field} integration has invalid type")
    profile = route.resolve_latest(registry)
    try:
        integration.verify_profile(profile)
    except BookmakerIntegrationEvidenceError as exc:
        raise ProviderFallbackPolicyError(
            f"{field} integration/profile mismatch"
        ) from exc
    if _instant(profile.observed_at, f"{field}.profile.observed_at") > decided_at:
        raise ProviderFallbackPolicyError(
            f"{field} capability profile is future at decided_at"
        )
    if _instant(integration.observed_at, f"{field}.integration.observed_at") > decided_at:
        raise ProviderFallbackPolicyError(
            f"{field} integration evidence is future at decided_at"
        )
    return profile


def _decision(
    *,
    disposition: ProviderFallbackDisposition,
    capability: BookmakerCapability,
    primary_route: ProviderRouteIdentity,
    primary: BookmakerCapabilityProfile,
    primary_health: _ResolvedHealth,
    policy: IngestionPolicy,
    decided_at: str,
    reason: str,
    selected_route: ProviderRouteIdentity | None = None,
    selected: BookmakerCapabilityProfile | None = None,
    selected_integration: BookmakerIntegrationEvidence | None = None,
    fallback_route: ProviderRouteIdentity | None = None,
    fallback: BookmakerCapabilityProfile | None = None,
    fallback_health: _ResolvedHealth | None = None,
) -> ProviderFallbackDecision:
    return ProviderFallbackDecision(
        disposition=disposition,
        required_capability=capability,
        primary_source_id=primary_route.source_id,
        primary_profile_id=primary.profile_id,
        primary_health_state=primary_health.state,
        primary_health_snapshot_id=primary_health.snapshot_id,
        selected_source_id=selected_route.source_id if selected_route else None,
        selected_profile_id=selected.profile_id if selected else None,
        selected_integration_evidence_id=(
            selected_integration.evidence_id if selected_integration else None
        ),
        fallback_source_id=fallback_route.source_id if fallback_route else None,
        fallback_profile_id=fallback.profile_id if fallback else None,
        fallback_health_state=fallback_health.state if fallback_health else None,
        fallback_health_snapshot_id=(
            fallback_health.snapshot_id if fallback_health else None
        ),
        health_stale_after_seconds=float(policy.stale_after_seconds),
        decided_at=decided_at,
        reason=reason,
    )


def resolve_readonly_provider_route(
    *,
    registry: BookmakerCapabilityRegistry,
    health_store: SourceHealthStore,
    required_capability: BookmakerCapability,
    primary_route: ProviderRouteIdentity,
    primary_integration: BookmakerIntegrationEvidence,
    decided_at: str,
    policy: IngestionPolicy | None = None,
    fallback_route: ProviderRouteIdentity | None = None,
    fallback_integration: BookmakerIntegrationEvidence | None = None,
) -> ProviderFallbackDecision:
    """Resolve technical read eligibility from durable capability and health truth."""

    if type(required_capability) is not BookmakerCapability:
        raise ProviderFallbackPolicyError(
            "required_capability must be BookmakerCapability"
        )
    if required_capability not in _READ_ONLY_CAPABILITIES:
        raise ProviderFallbackPolicyError(
            "fallback policy is restricted to read-only capabilities"
        )
    resolved_policy = policy or IngestionPolicy()
    if not isinstance(resolved_policy, IngestionPolicy):
        raise ProviderFallbackPolicyError("policy must be IngestionPolicy")
    decision_at = _instant(decided_at, "decided_at")

    supplied = (
        fallback_route is not None,
        fallback_integration is not None,
    )
    if any(supplied) and not all(supplied):
        raise ProviderFallbackPolicyError(
            "fallback route and integration evidence must be supplied together"
        )

    primary = _resolve_profile(
        registry=registry,
        route=primary_route,
        integration=primary_integration,
        decided_at=decision_at,
        field="primary",
    )
    primary_health = _resolve_health(
        health_store=health_store,
        source_id=primary_route.source_id,
        decided_at=decision_at,
        policy=resolved_policy,
    )
    primary_capability = primary.state_of(required_capability)

    if primary_health.state is ProviderOperationalState.HEALTHY:
        if primary_capability is BookmakerCapabilityState.SUPPORTED:
            return _decision(
                disposition=ProviderFallbackDisposition.PRIMARY_TECHNICALLY_ELIGIBLE,
                capability=required_capability,
                primary_route=primary_route,
                primary=primary,
                primary_health=primary_health,
                selected_route=primary_route,
                selected=primary,
                selected_integration=primary_integration,
                policy=resolved_policy,
                decided_at=decided_at,
                reason="PRIMARY_HEALTHY_SUPPORTED",
            )
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary_route=primary_route,
            primary=primary,
            primary_health=primary_health,
            policy=resolved_policy,
            decided_at=decided_at,
            reason=f"PRIMARY_HEALTHY_CAPABILITY_{primary_capability.value.upper()}",
        )

    if not all(supplied):
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary_route=primary_route,
            primary=primary,
            primary_health=primary_health,
            policy=resolved_policy,
            decided_at=decided_at,
            reason=f"PRIMARY_{primary_health.state.value}_NO_FALLBACK",
        )

    assert fallback_route is not None
    assert fallback_integration is not None
    if (
        fallback_route.source_id == primary_route.source_id
        and fallback_route.profile_id == primary_route.profile_id
    ):
        raise ProviderFallbackPolicyError(
            "fallback must not reuse exact primary source/profile"
        )

    fallback = _resolve_profile(
        registry=registry,
        route=fallback_route,
        integration=fallback_integration,
        decided_at=decision_at,
        field="fallback",
    )
    fallback_health = _resolve_health(
        health_store=health_store,
        source_id=fallback_route.source_id,
        decided_at=decision_at,
        policy=resolved_policy,
    )

    if (
        required_capability in _ACCOUNT_SCOPED_READ_CAPABILITIES
        and (fallback.venue_id, fallback.account_id)
        != (primary.venue_id, primary.account_id)
    ):
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary_route=primary_route,
            primary=primary,
            primary_health=primary_health,
            fallback_route=fallback_route,
            fallback=fallback,
            fallback_health=fallback_health,
            policy=resolved_policy,
            decided_at=decided_at,
            reason="ACCOUNT_SCOPED_FALLBACK_IDENTITY_MISMATCH",
        )

    if fallback_health.state is not ProviderOperationalState.HEALTHY:
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary_route=primary_route,
            primary=primary,
            primary_health=primary_health,
            fallback_route=fallback_route,
            fallback=fallback,
            fallback_health=fallback_health,
            policy=resolved_policy,
            decided_at=decided_at,
            reason=f"FALLBACK_{fallback_health.state.value}",
        )

    fallback_capability = fallback.state_of(required_capability)
    if fallback_capability is not BookmakerCapabilityState.SUPPORTED:
        return _decision(
            disposition=ProviderFallbackDisposition.ABSTAIN,
            capability=required_capability,
            primary_route=primary_route,
            primary=primary,
            primary_health=primary_health,
            fallback_route=fallback_route,
            fallback=fallback,
            fallback_health=fallback_health,
            policy=resolved_policy,
            decided_at=decided_at,
            reason=f"FALLBACK_CAPABILITY_{fallback_capability.value.upper()}",
        )

    return _decision(
        disposition=ProviderFallbackDisposition.FALLBACK_TECHNICALLY_ELIGIBLE,
        capability=required_capability,
        primary_route=primary_route,
        primary=primary,
        primary_health=primary_health,
        selected_route=fallback_route,
        selected=fallback,
        selected_integration=fallback_integration,
        fallback_route=fallback_route,
        fallback=fallback,
        fallback_health=fallback_health,
        policy=resolved_policy,
        decided_at=decided_at,
        reason=(
            f"PRIMARY_{primary_health.state.value}_"
            "FALLBACK_HEALTHY_SUPPORTED"
        ),
    )
