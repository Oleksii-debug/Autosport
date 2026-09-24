"""Explicit evidence for bookmaker integration transport without widening authority.

The technical capability profile answers *what* an adapter has proved it can observe or
support.  This module answers only *how* that adapter is integrated (official API versus
browser automation) and binds that statement to one exact capability-profile identity.
It deliberately contains no credentials, network client, provider mutation, legal/terms
permission, execution, readiness, or real-money authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import BookmakerCapabilityProfile


class BookmakerIntegrationEvidenceError(ValueError):
    """Raised when integration-channel evidence is malformed or drifts."""


class BookmakerIntegrationKind(str, Enum):
    """Mechanism used by an adapter; not a capability or permission level."""

    OFFICIAL_API = "official_api"
    BROWSER_AUTOMATION = "browser_automation"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BookmakerIntegrationEvidenceError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise BookmakerIntegrationEvidenceError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BookmakerIntegrationEvidenceError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BookmakerIntegrationEvidenceError(
            f"{field} must include a timezone offset"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class BookmakerIntegrationEvidence:
    """Immutable proof of one adapter integration mechanism for one exact profile.

    ``integration_kind`` is descriptive evidence only.  In particular, an official API
    does not imply write support, and browser automation does not imply legal permission,
    account authority, or any execution capability.
    """

    venue_id: str
    adapter_id: str
    adapter_version: str
    profile_id: str
    integration_kind: BookmakerIntegrationKind
    observed_at: str
    source_ref: str
    source_payload_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.adapter_version, "adapter_version")
        _sha256(self.profile_id, "profile_id")
        if type(self.integration_kind) is not BookmakerIntegrationKind:
            raise BookmakerIntegrationEvidenceError(
                "integration_kind must be a BookmakerIntegrationKind value"
            )
        _timestamp(self.observed_at, "observed_at")
        _text(self.source_ref, "source_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise BookmakerIntegrationEvidenceError(
                "schema_version must be exactly 1"
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

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "integration_kind": self.integration_kind.value,
            "observed_at": self.observed_at,
            "profile_id": self.profile_id,
            "schema_version": self.schema_version,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
            "venue_id": self.venue_id,
        }

    def verify_profile(self, profile: BookmakerCapabilityProfile) -> None:
        """Fail closed unless this evidence names the exact supplied profile."""

        if type(profile) is not BookmakerCapabilityProfile:
            raise BookmakerIntegrationEvidenceError(
                "profile must be an exact BookmakerCapabilityProfile"
            )
        expected = (
            profile.venue_id,
            profile.adapter_id,
            profile.adapter_version,
            profile.profile_id,
        )
        actual = (
            self.venue_id,
            self.adapter_id,
            self.adapter_version,
            self.profile_id,
        )
        if actual != expected:
            raise BookmakerIntegrationEvidenceError(
                "integration evidence does not match capability profile identity"
            )
        if _timestamp(self.observed_at, "observed_at") < _timestamp(
            profile.observed_at, "profile.observed_at"
        ):
            raise BookmakerIntegrationEvidenceError(
                "integration evidence cannot predate the capability profile it binds"
            )


def bind_bookmaker_integration(
    profile: BookmakerCapabilityProfile,
    *,
    integration_kind: BookmakerIntegrationKind,
    observed_at: str,
    source_ref: str,
    source_payload_sha256: str,
) -> BookmakerIntegrationEvidence:
    """Create and immediately verify channel evidence for one exact profile."""

    if type(profile) is not BookmakerCapabilityProfile:
        raise BookmakerIntegrationEvidenceError(
            "profile must be an exact BookmakerCapabilityProfile"
        )
    evidence = BookmakerIntegrationEvidence(
        venue_id=profile.venue_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_id=profile.profile_id,
        integration_kind=integration_kind,
        observed_at=observed_at,
        source_ref=source_ref,
        source_payload_sha256=source_payload_sha256,
    )
    evidence.verify_profile(profile)
    return evidence
