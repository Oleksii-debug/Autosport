"""Fail-closed provider degradation evidence.

This module records *why* one provider/account/adapter capability could not provide
normal evidence at a point in time.  It is intentionally policy/data only: recording a
failure never authorizes provider writes, real-money execution, or an automatic switch to
a different provider whose market/account semantics have not been independently proven.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import BookmakerCapability


PROVIDER_DEGRADATION_SCHEMA_VERSION = 1


class ProviderDegradationError(ValueError):
    """Raised when provider degradation evidence is ambiguous or malformed."""


class ProviderDegradationReason(str, Enum):
    TRANSIENT_PROVIDER_FAILURE = "transient_provider_failure"
    STALE_DATA = "stale_data"
    MALFORMED_EVIDENCE = "malformed_evidence"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION_OR_PERMISSION_FAILURE = "authentication_or_permission_failure"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    UNKNOWN = "unknown"


class ProviderRecoveryClass(str, Enum):
    """Diagnostic recovery class, not permission to execute a retry or fallback."""

    NO_AUTOMATIC_RETRY = "no_automatic_retry"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    REFRESH_EVIDENCE = "refresh_evidence"
    REAUTHORIZE = "reauthorize"
    OPERATOR_REVIEW = "operator_review"


_RECOVERY_CLASS = {
    ProviderDegradationReason.TRANSIENT_PROVIDER_FAILURE: ProviderRecoveryClass.RETRY_WITH_BACKOFF,
    ProviderDegradationReason.STALE_DATA: ProviderRecoveryClass.REFRESH_EVIDENCE,
    ProviderDegradationReason.MALFORMED_EVIDENCE: ProviderRecoveryClass.OPERATOR_REVIEW,
    ProviderDegradationReason.RATE_LIMITED: ProviderRecoveryClass.RETRY_WITH_BACKOFF,
    ProviderDegradationReason.AUTHENTICATION_OR_PERMISSION_FAILURE: ProviderRecoveryClass.REAUTHORIZE,
    ProviderDegradationReason.TRANSPORT_UNAVAILABLE: ProviderRecoveryClass.RETRY_WITH_BACKOFF,
    ProviderDegradationReason.UNKNOWN: ProviderRecoveryClass.OPERATOR_REVIEW,
}


def _text(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderDegradationError(f"{field} must be a non-empty trimmed string")
    return value


def _timestamp(value: str, field: str) -> datetime:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProviderDegradationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderDegradationError(f"{field} must include a timezone offset")
    return parsed


def _sha256(value: str, field: str) -> str:
    _text(value, field)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ProviderDegradationError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProviderDegradationEvent:
    """One immutable provider capability degradation observation.

    ``recovery_class`` is derived solely from the reason vocabulary.  It describes what a
    higher layer would need to consider, but does not itself authorize retries, provider
    substitution, credentials use, writes, or execution.
    """

    venue_id: str
    account_id: str
    adapter_id: str
    capability: BookmakerCapability
    reason: ProviderDegradationReason
    observed_at: str
    evidence_ref: str
    source_payload_sha256: str
    detail_code: str
    schema_version: int = PROVIDER_DEGRADATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        if type(self.capability) is not BookmakerCapability:
            raise ProviderDegradationError(
                "capability must be an exact BookmakerCapability value"
            )
        if type(self.reason) is not ProviderDegradationReason:
            raise ProviderDegradationError(
                "reason must be an exact ProviderDegradationReason value"
            )
        _timestamp(self.observed_at, "observed_at")
        _text(self.evidence_ref, "evidence_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")
        _text(self.detail_code, "detail_code")
        if (
            type(self.schema_version) is not int
            or self.schema_version != PROVIDER_DEGRADATION_SCHEMA_VERSION
        ):
            raise ProviderDegradationError(
                f"schema_version must be integer {PROVIDER_DEGRADATION_SCHEMA_VERSION}"
            )

    @property
    def recovery_class(self) -> ProviderRecoveryClass:
        return _RECOVERY_CLASS[self.reason]

    @property
    def automatic_fallback_authorized(self) -> bool:
        """A degradation observation cannot prove cross-provider substitution safety."""

        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    @property
    def event_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "automatic_fallback_authorized": self.automatic_fallback_authorized,
            "capability": self.capability.value,
            "detail_code": self.detail_code,
            "evidence_ref": self.evidence_ref,
            "observed_at": self.observed_at,
            "provider_write_authorized": self.provider_write_authorized,
            "real_money_execution": self.real_money_execution,
            "reason": self.reason.value,
            "recovery_class": self.recovery_class.value,
            "schema_version": self.schema_version,
            "source_payload_sha256": self.source_payload_sha256,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class ProviderDegradationReport:
    """Deterministic point-in-time evidence set for degraded provider capabilities."""

    events: tuple[ProviderDegradationEvent, ...]
    as_of: str
    schema_version: int = PROVIDER_DEGRADATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PROVIDER_DEGRADATION_SCHEMA_VERSION
        ):
            raise ProviderDegradationError(
                f"schema_version must be integer {PROVIDER_DEGRADATION_SCHEMA_VERSION}"
            )
        if type(self.events) is not tuple:
            raise ProviderDegradationError("events must be a tuple")
        if not self.events:
            raise ProviderDegradationError("events must not be empty")
        as_of = _timestamp(self.as_of, "as_of")
        identities: set[str] = set()
        scopes: set[tuple[str, str, str, BookmakerCapability]] = set()
        for event in self.events:
            if type(event) is not ProviderDegradationEvent:
                raise ProviderDegradationError(
                    "events must contain exact ProviderDegradationEvent values"
                )
            if _timestamp(event.observed_at, "event.observed_at") > as_of:
                raise ProviderDegradationError(
                    "degradation event observation cannot be after report as_of"
                )
            if event.event_id in identities:
                raise ProviderDegradationError(
                    f"duplicate degradation event identity: {event.event_id}"
                )
            identities.add(event.event_id)
            scope = (
                event.venue_id,
                event.account_id,
                event.adapter_id,
                event.capability,
            )
            if scope in scopes:
                raise ProviderDegradationError(
                    "multiple degradation outcomes for one venue/account/adapter/capability "
                    "scope require a newer report rather than ambiguous same-report evidence"
                )
            scopes.add(scope)

    @property
    def report_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @property
    def automatic_fallback_authorized(self) -> bool:
        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of,
            "automatic_fallback_authorized": self.automatic_fallback_authorized,
            "events": [
                event.to_canonical_dict()
                for event in sorted(
                    self.events,
                    key=lambda item: (
                        item.venue_id,
                        item.account_id,
                        item.adapter_id,
                        item.capability.value,
                        item.observed_at,
                        item.event_id,
                    ),
                )
            ],
            "provider_write_authorized": self.provider_write_authorized,
            "real_money_execution": self.real_money_execution,
            "schema_version": self.schema_version,
        }
