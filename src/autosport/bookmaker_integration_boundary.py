"""Descriptive bookmaker integration-channel evidence.

This module records how one exact bookmaker adapter/profile is integrated. It is
not a capability, legal-permission, credential, provider-write, or execution
authority. Choosing an official API rather than browser automation must never
silently upgrade the technical capability profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import BookmakerCapabilityProfile


class BookmakerIntegrationBoundaryError(ValueError):
    """Raised when integration-channel evidence violates the fail-closed contract."""


class BookmakerIntegrationChannel(str, Enum):
    """Transport/integration mechanism only; never an authority level."""

    OFFICIAL_API = "official_api"
    BROWSER_AUTOMATION = "browser_automation"


def _text(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BookmakerIntegrationBoundaryError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _timestamp(value: str, field: str) -> datetime:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BookmakerIntegrationBoundaryError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BookmakerIntegrationBoundaryError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _sha256(value: str, field: str) -> str:
    _text(value, field)
    if (
        len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BookmakerIntegrationBoundaryError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return value


@dataclass(frozen=True, slots=True)
class BookmakerIntegrationChannelEvidence:
    """Immutable channel evidence bound to one exact capability profile.

    source_ref and source_payload_sha256 identify the evidence establishing the
    integration mechanism itself. They do not establish provider capabilities,
    contractual/legal permission, authentication material, or permission to
    perform external effects.
    """

    profile: BookmakerCapabilityProfile
    channel: BookmakerIntegrationChannel
    observed_at: str
    source_ref: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        if type(self.profile) is not BookmakerCapabilityProfile:
            raise BookmakerIntegrationBoundaryError(
                "profile must be an exact BookmakerCapabilityProfile"
            )
        if type(self.channel) is not BookmakerIntegrationChannel:
            raise BookmakerIntegrationBoundaryError(
                "channel must be a BookmakerIntegrationChannel value"
            )
        _timestamp(self.observed_at, "observed_at")
        _text(self.source_ref, "source_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id

    @property
    def evidence_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def to_canonical_dict(self) -> dict[str, object]:
        """Return deterministic descriptive evidence with explicit false authorities."""

        return {
            "account_id": self.profile.account_id,
            "adapter_id": self.profile.adapter_id,
            "adapter_version": self.profile.adapter_version,
            "channel": self.channel.value,
            "credentials_proven": False,
            "execution_authorized": False,
            "legal_terms_permission_proven": False,
            "observed_at": self.observed_at,
            "profile_id": self.profile.profile_id,
            "profile_source_payload_sha256": self.profile.source_payload_sha256,
            "profile_source_ref": self.profile.source_ref,
            "profile_version": self.profile.profile_version,
            "provider_write_authorized": False,
            "real_money_execution": False,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
            "technical_capabilities_authorized": False,
            "venue_id": self.profile.venue_id,
        }

    def verify_profile(self, candidate: BookmakerCapabilityProfile) -> None:
        """Fail closed unless candidate is the exact profile this evidence binds."""

        if type(candidate) is not BookmakerCapabilityProfile:
            raise BookmakerIntegrationBoundaryError(
                "candidate must be an exact BookmakerCapabilityProfile"
            )
        expected = (
            self.profile.venue_id,
            self.profile.account_id,
            self.profile.adapter_id,
            self.profile.adapter_version,
            self.profile.profile_version,
            self.profile.profile_id,
            self.profile.source_ref,
            self.profile.source_payload_sha256,
        )
        actual = (
            candidate.venue_id,
            candidate.account_id,
            candidate.adapter_id,
            candidate.adapter_version,
            candidate.profile_version,
            candidate.profile_id,
            candidate.source_ref,
            candidate.source_payload_sha256,
        )
        if actual != expected:
            raise BookmakerIntegrationBoundaryError(
                "capability profile does not match integration-channel evidence"
            )

    @property
    def technical_capabilities_authorized(self) -> bool:
        return False

    @property
    def legal_terms_permission_proven(self) -> bool:
        return False

    @property
    def credentials_proven(self) -> bool:
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
