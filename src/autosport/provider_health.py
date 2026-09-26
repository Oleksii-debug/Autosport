"""Fail-closed descriptive provider health and capability-drift evidence.

This module records provider-health observations against one exact canonical
``BookmakerCapabilityProfile`` identity. It deliberately does not turn a
caller-supplied HEALTHY label into provider-origin, quote-freshness, execution,
or real-money authority. Restrictive states remain explicit, and a semantic
capability change mechanically requires downstream certification
requalification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)


class ProviderHealthEvidenceError(ValueError):
    """Raised when provider-health or capability-drift evidence is malformed."""


class ProviderHealthState(str, Enum):
    """Descriptive provider health vocabulary; never execution authority."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    MAPPING_CONFLICT = "mapping_conflict"
    CAPABILITY_CHANGED = "capability_changed"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderHealthEvidenceError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProviderHealthEvidenceError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderHealthEvidenceError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ProviderHealthEvidenceError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _validate_exact_profile(profile: BookmakerCapabilityProfile) -> None:
    if type(profile) is not BookmakerCapabilityProfile:
        raise ProviderHealthEvidenceError(
            "profile must be an exact BookmakerCapabilityProfile"
        )
    if type(profile.facts) is not tuple:
        raise ProviderHealthEvidenceError("profile.facts must be an exact tuple")
    for fact in profile.facts:
        if type(fact) is not BookmakerCapabilityFact:
            raise ProviderHealthEvidenceError(
                "profile facts must be exact BookmakerCapabilityFact values"
            )


def _validate_profile_progression(
    previous: BookmakerCapabilityProfile,
    current: BookmakerCapabilityProfile,
) -> None:
    previous_scope = (
        previous.venue_id,
        previous.account_id,
        previous.adapter_id,
        previous.adapter_version,
    )
    current_scope = (
        current.venue_id,
        current.account_id,
        current.adapter_id,
        current.adapter_version,
    )
    if current_scope != previous_scope:
        raise ProviderHealthEvidenceError(
            "capability drift profiles must describe one exact "
            "venue/account/adapter/version scope"
        )
    if current.profile_version <= previous.profile_version:
        raise ProviderHealthEvidenceError(
            "current profile_version must advance for capability drift"
        )
    if _timestamp(current.observed_at, "current.observed_at") < _timestamp(
        previous.observed_at,
        "previous.observed_at",
    ):
        raise ProviderHealthEvidenceError(
            "current capability profile cannot predate previous profile"
        )


def _semantic_changed_capabilities(
    previous: BookmakerCapabilityProfile,
    current: BookmakerCapabilityProfile,
) -> tuple[BookmakerCapability, ...]:
    return tuple(
        sorted(
            (
                capability
                for capability in BookmakerCapability
                if previous.state_of(capability)
                is not current.state_of(capability)
            ),
            key=lambda item: item.value,
        )
    )


@dataclass(frozen=True, slots=True)
class ProviderHealthEvidence:
    """One immutable descriptive health observation for one exact profile.

    Positive HEALTHY state is intentionally *not* proof of provider origin,
    quote freshness, or permission to execute. Consumers may use restrictive
    states to fail closed, but must compose separate canonical evidence before
    treating health restoration as authoritative.
    """

    profile: BookmakerCapabilityProfile
    state: ProviderHealthState
    since: str
    observed_at: str
    reason: str
    source_ref: str
    source_payload_sha256: str
    previous_profile_id: str | None = None
    changed_capabilities: tuple[BookmakerCapability, ...] = ()
    previous_profile: BookmakerCapabilityProfile | None = None

    def __post_init__(self) -> None:
        _validate_exact_profile(self.profile)
        if type(self.state) is not ProviderHealthState:
            raise ProviderHealthEvidenceError(
                "state must be a ProviderHealthState value"
            )

        profile_observed = _timestamp(
            self.profile.observed_at,
            "profile.observed_at",
        )
        since = _timestamp(self.since, "since")
        observed = _timestamp(self.observed_at, "observed_at")
        if since > observed:
            raise ProviderHealthEvidenceError("since cannot be later than observed_at")
        if observed < profile_observed:
            raise ProviderHealthEvidenceError(
                "provider health observed_at cannot predate the capability profile"
            )

        _text(self.reason, "reason")
        _text(self.source_ref, "source_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")

        if type(self.changed_capabilities) is not tuple:
            raise ProviderHealthEvidenceError(
                "changed_capabilities must be an exact tuple"
            )
        for capability in self.changed_capabilities:
            if type(capability) is not BookmakerCapability:
                raise ProviderHealthEvidenceError(
                    "changed_capabilities must contain exact BookmakerCapability values"
                )
        if len(set(self.changed_capabilities)) != len(self.changed_capabilities):
            raise ProviderHealthEvidenceError(
                "changed_capabilities cannot contain duplicates"
            )
        canonical_changes = tuple(
            sorted(self.changed_capabilities, key=lambda item: item.value)
        )
        if self.changed_capabilities != canonical_changes:
            raise ProviderHealthEvidenceError(
                "changed_capabilities must be sorted by capability value"
            )

        if self.state is ProviderHealthState.CAPABILITY_CHANGED:
            if self.previous_profile_id is None:
                raise ProviderHealthEvidenceError(
                    "capability_changed requires previous_profile_id"
                )
            previous_profile_id = _sha256(
                self.previous_profile_id,
                "previous_profile_id",
            )
            if previous_profile_id == self.profile.profile_id:
                raise ProviderHealthEvidenceError(
                    "previous_profile_id must differ from current profile_id"
                )
            if self.previous_profile is None:
                raise ProviderHealthEvidenceError(
                    "capability_changed requires previous_profile"
                )
            _validate_exact_profile(self.previous_profile)
            if self.previous_profile.profile_id != previous_profile_id:
                raise ProviderHealthEvidenceError(
                    "previous_profile_id must match previous_profile.profile_id"
                )
            _validate_profile_progression(self.previous_profile, self.profile)
            actual_changes = _semantic_changed_capabilities(
                self.previous_profile,
                self.profile,
            )
            if not actual_changes:
                raise ProviderHealthEvidenceError(
                    "profiles contain no semantic capability-state change"
                )
            if self.changed_capabilities != actual_changes:
                raise ProviderHealthEvidenceError(
                    "changed_capabilities must equal the exact semantic profile delta"
                )
            if since < profile_observed:
                raise ProviderHealthEvidenceError(
                    "capability change cannot be backdated before current profile evidence"
                )
        else:
            if self.previous_profile_id is not None:
                raise ProviderHealthEvidenceError(
                    "previous_profile_id is only valid for capability_changed"
                )
            if self.changed_capabilities:
                raise ProviderHealthEvidenceError(
                    "changed_capabilities are only valid for capability_changed"
                )
            if self.previous_profile is not None:
                raise ProviderHealthEvidenceError(
                    "previous_profile is only valid for capability_changed"
                )

    @property
    def evidence_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @property
    def is_degraded(self) -> bool:
        """Whether this observation names a restrictive/non-healthy state."""

        return self.state is not ProviderHealthState.HEALTHY

    @property
    def certification_requalification_required(self) -> bool:
        """Capability semantic drift invalidates prior capability certification."""

        return self.state is ProviderHealthState.CAPABILITY_CHANGED

    @property
    def provider_origin_proven(self) -> bool:
        return False

    @property
    def healthy_authority(self) -> bool:
        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.profile.account_id,
            "adapter_id": self.profile.adapter_id,
            "adapter_version": self.profile.adapter_version,
            "changed_capabilities": [
                capability.value for capability in self.changed_capabilities
            ],
            "observed_at": self.observed_at,
            "previous_profile_id": self.previous_profile_id,
            "profile_id": self.profile.profile_id,
            "profile_version": self.profile.profile_version,
            "reason": self.reason,
            "since": self.since,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
            "state": self.state.value,
            "venue_id": self.profile.venue_id,
        }


def detect_capability_change(
    previous: BookmakerCapabilityProfile,
    current: BookmakerCapabilityProfile,
    *,
    since: str,
    observed_at: str,
    reason: str,
    source_ref: str,
    source_payload_sha256: str,
) -> ProviderHealthEvidence:
    """Build fail-closed CAPABILITY_CHANGED evidence from two exact profiles.

    Profile IDs can change for non-semantic reasons such as a later observation
    timestamp. This helper therefore emits drift evidence only when at least one
    canonical ``BookmakerCapabilityState`` actually changes.
    """

    _validate_exact_profile(previous)
    _validate_exact_profile(current)
    _validate_profile_progression(previous, current)

    changed = _semantic_changed_capabilities(previous, current)
    if not changed:
        raise ProviderHealthEvidenceError(
            "profiles contain no semantic capability-state change"
        )

    return ProviderHealthEvidence(
        profile=current,
        state=ProviderHealthState.CAPABILITY_CHANGED,
        since=since,
        observed_at=observed_at,
        reason=reason,
        source_ref=source_ref,
        source_payload_sha256=source_payload_sha256,
        previous_profile_id=previous.profile_id,
        changed_capabilities=changed,
        previous_profile=previous,
    )
