from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


_SHA256_HEX_LENGTH = 64


class ObservationLatencyClass(str, Enum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    HISTORICAL = "HISTORICAL"
    UNKNOWN = "UNKNOWN"


class EvidenceUse(str, Enum):
    INTERNAL_RESEARCH = "INTERNAL_RESEARCH"
    FORWARD_ECONOMIC_PROOF = "FORWARD_ECONOMIC_PROOF"
    LIVE_EXECUTION = "LIVE_EXECUTION"


class EntitlementPermission(str, Enum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    UNKNOWN = "UNKNOWN"


class EntitlementVerification(str, Enum):
    VERIFIED_DOCUMENT_BOUND = "VERIFIED_DOCUMENT_BOUND"
    UNVERIFIED = "UNVERIFIED"


class CausalEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EntitlementGrant:
    use: EvidenceUse
    permission: EntitlementPermission

    def __post_init__(self) -> None:
        if not isinstance(self.use, EvidenceUse):
            raise TypeError("entitlement grant use must be EvidenceUse")
        if not isinstance(self.permission, EntitlementPermission):
            raise TypeError("entitlement grant permission must be EntitlementPermission")


@dataclass(frozen=True, slots=True)
class ProviderEntitlementProfile:
    profile_id: str
    provider_id: str
    product_id: str
    access_level: str
    grants: tuple[EntitlementGrant, ...]
    retention_constraints: tuple[str, ...]
    redistribution_constraints: tuple[str, ...]
    jurisdiction_account_prerequisites: tuple[str, ...]
    rate_quota_class: str
    effective_from: str
    effective_to: str | None
    source_document_identity: str
    source_document_sha256: str
    verification: EntitlementVerification = EntitlementVerification.UNVERIFIED

    def __post_init__(self) -> None:
        _require_token(self.profile_id, "profile_id")
        _require_token(self.provider_id, "provider_id")
        _require_text(self.product_id, "product_id")
        _require_text(self.access_level, "access_level")
        _require_text(self.rate_quota_class, "rate_quota_class")
        _require_text(self.source_document_identity, "source_document_identity")
        _require_sha256(self.source_document_sha256, "source_document_sha256")
        if not isinstance(self.verification, EntitlementVerification):
            raise TypeError("verification must be EntitlementVerification")
        if type(self.grants) is not tuple:
            raise TypeError("grants must be tuple")
        if not self.grants:
            raise ValueError("grants must explicitly cover every EvidenceUse")
        seen: set[EvidenceUse] = set()
        for grant in self.grants:
            if type(grant) is not EntitlementGrant:
                raise TypeError("grants must contain EntitlementGrant values")
            if grant.use in seen:
                raise ValueError(f"duplicate entitlement grant for {grant.use.value}")
            seen.add(grant.use)
        if seen != set(EvidenceUse):
            missing = sorted(use.value for use in set(EvidenceUse) - seen)
            raise ValueError(f"grants must explicitly cover every EvidenceUse; missing={missing}")
        _require_text_tuple(self.retention_constraints, "retention_constraints")
        _require_text_tuple(self.redistribution_constraints, "redistribution_constraints")
        _require_text_tuple(
            self.jurisdiction_account_prerequisites,
            "jurisdiction_account_prerequisites",
        )
        effective_from = _parse_timestamp(self.effective_from, "effective_from")
        if self.effective_to is not None:
            effective_to = _parse_timestamp(self.effective_to, "effective_to")
            if effective_to < effective_from:
                raise ValueError("effective_to must not precede effective_from")

    def permission_for(self, use: EvidenceUse) -> EntitlementPermission:
        if not isinstance(use, EvidenceUse):
            raise TypeError("use must be EvidenceUse")
        for grant in self.grants:
            if grant.use is use:
                return grant.permission
        raise AssertionError("validated entitlement profile lost an EvidenceUse grant")

    @property
    def canonical_sha256(self) -> str:
        return _canonical_sha256(
            {
                "schema": "provider-entitlement-profile-v1",
                "profile_id": self.profile_id,
                "provider_id": self.provider_id,
                "product_id": self.product_id,
                "access_level": self.access_level,
                "grants": [
                    {"use": grant.use.value, "permission": grant.permission.value}
                    for grant in sorted(self.grants, key=lambda item: item.use.value)
                ],
                "retention_constraints": list(self.retention_constraints),
                "redistribution_constraints": list(self.redistribution_constraints),
                "jurisdiction_account_prerequisites": list(
                    self.jurisdiction_account_prerequisites
                ),
                "rate_quota_class": self.rate_quota_class,
                "effective_from": self.effective_from,
                "effective_to": self.effective_to,
                "source_document_identity": self.source_document_identity,
                "source_document_sha256": self.source_document_sha256,
                "verification": self.verification.value,
            }
        )


@dataclass(frozen=True, slots=True)
class CausalObservationEnvelope:
    observation_id: str
    provider_id: str
    provider_event_id: str
    market_id: str
    subject_id: str
    endpoint_or_feed: str
    provider_schema_version: str
    received_at_utc: str
    receipt_clock_id: str
    receipt_ordinal: int
    latency_class: ObservationLatencyClass
    payload_sha256: str
    raw_artifact_ref: str
    entitlement_profile_id: str
    provider_published_at: str | None = None
    provider_revision_or_market_version: str | None = None
    identity_mapping_version: str | None = None
    mapping_observed_at: str | None = None
    correction_of_observation_id: str | None = None

    def __post_init__(self) -> None:
        _require_token(self.observation_id, "observation_id")
        _require_token(self.provider_id, "provider_id")
        _require_token(self.provider_event_id, "provider_event_id")
        _require_token(self.market_id, "market_id")
        _require_token(self.subject_id, "subject_id")
        _require_text(self.endpoint_or_feed, "endpoint_or_feed")
        _require_text(self.provider_schema_version, "provider_schema_version")
        _parse_timestamp(self.received_at_utc, "received_at_utc")
        _require_token(self.receipt_clock_id, "receipt_clock_id")
        if type(self.receipt_ordinal) is not int or self.receipt_ordinal < 0:
            raise ValueError("receipt_ordinal must be a non-negative non-boolean int")
        if not isinstance(self.latency_class, ObservationLatencyClass):
            raise TypeError("latency_class must be ObservationLatencyClass")
        _require_sha256(self.payload_sha256, "payload_sha256")
        _require_text(self.raw_artifact_ref, "raw_artifact_ref")
        _require_token(self.entitlement_profile_id, "entitlement_profile_id")
        if self.provider_published_at is not None:
            _parse_timestamp(self.provider_published_at, "provider_published_at")
        if self.provider_revision_or_market_version is not None:
            _require_text(
                self.provider_revision_or_market_version,
                "provider_revision_or_market_version",
            )
        mapping_pair = (self.identity_mapping_version, self.mapping_observed_at)
        if (mapping_pair[0] is None) != (mapping_pair[1] is None):
            raise ValueError(
                "identity_mapping_version and mapping_observed_at must be present together"
            )
        if self.identity_mapping_version is not None:
            _require_text(self.identity_mapping_version, "identity_mapping_version")
            _parse_timestamp(self.mapping_observed_at, "mapping_observed_at")
        if self.correction_of_observation_id is not None:
            _require_token(
                self.correction_of_observation_id,
                "correction_of_observation_id",
            )
            if self.correction_of_observation_id == self.observation_id:
                raise ValueError("observation cannot correct itself")

    @property
    def payload_identity_sha256(self) -> str:
        """Stable duplicate key that intentionally excludes receipt chronology."""
        return _canonical_sha256(
            {
                "schema": "provider-payload-identity-v1",
                "provider_id": self.provider_id,
                "endpoint_or_feed": self.endpoint_or_feed,
                "payload_sha256": self.payload_sha256,
            }
        )

    @property
    def canonical_sha256(self) -> str:
        """Immutable observation identity that includes receipt chronology."""
        return _canonical_sha256(
            {
                "schema": "causal-observation-envelope-v1",
                "observation_id": self.observation_id,
                "provider_id": self.provider_id,
                "provider_event_id": self.provider_event_id,
                "market_id": self.market_id,
                "subject_id": self.subject_id,
                "endpoint_or_feed": self.endpoint_or_feed,
                "provider_schema_version": self.provider_schema_version,
                "provider_published_at": self.provider_published_at,
                "received_at_utc": self.received_at_utc,
                "receipt_clock_id": self.receipt_clock_id,
                "receipt_ordinal": self.receipt_ordinal,
                "provider_revision_or_market_version": (
                    self.provider_revision_or_market_version
                ),
                "latency_class": self.latency_class.value,
                "payload_sha256": self.payload_sha256,
                "raw_artifact_ref": self.raw_artifact_ref,
                "entitlement_profile_id": self.entitlement_profile_id,
                "identity_mapping_version": self.identity_mapping_version,
                "mapping_observed_at": self.mapping_observed_at,
                "correction_of_observation_id": self.correction_of_observation_id,
            }
        )


@dataclass(frozen=True, slots=True)
class CausalEligibilityDecision:
    eligibility: CausalEligibility
    reason: str
    use: EvidenceUse
    decision_cutoff_utc: str
    observation_sha256: str
    entitlement_profile_sha256: str
    quarantined: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.eligibility, CausalEligibility):
            raise TypeError("eligibility must be CausalEligibility")
        _require_token(self.reason, "reason")
        if not isinstance(self.use, EvidenceUse):
            raise TypeError("use must be EvidenceUse")
        _parse_timestamp(self.decision_cutoff_utc, "decision_cutoff_utc")
        _require_sha256(self.observation_sha256, "observation_sha256")
        _require_sha256(
            self.entitlement_profile_sha256,
            "entitlement_profile_sha256",
        )
        if type(self.quarantined) is not bool:
            raise TypeError("quarantined must be bool")


def evaluate_causal_eligibility(
    observation: CausalObservationEnvelope,
    *,
    entitlement: ProviderEntitlementProfile,
    use: EvidenceUse,
    decision_cutoff_utc: str,
) -> CausalEligibilityDecision:
    """Evaluate causal/use eligibility without upgrading provider or legal authority.

    The function treats the supplied document-bound entitlement profile as evidence to
    be checked, not as a legal conclusion. Positive consumers still need a canonical
    product-owned profile store/issuance boundary before this value can authorize any
    production or money-moving action.
    """

    if type(observation) is not CausalObservationEnvelope:
        raise TypeError("observation must be CausalObservationEnvelope")
    if type(entitlement) is not ProviderEntitlementProfile:
        raise TypeError("entitlement must be ProviderEntitlementProfile")
    if not isinstance(use, EvidenceUse):
        raise TypeError("use must be EvidenceUse")

    cutoff = _parse_timestamp(decision_cutoff_utc, "decision_cutoff_utc")
    received = _parse_timestamp(observation.received_at_utc, "received_at_utc")
    profile_sha = entitlement.canonical_sha256
    observation_sha = observation.canonical_sha256

    def decision(
        eligibility: CausalEligibility,
        reason: str,
        *,
        quarantined: bool = False,
    ) -> CausalEligibilityDecision:
        return CausalEligibilityDecision(
            eligibility=eligibility,
            reason=reason,
            use=use,
            decision_cutoff_utc=decision_cutoff_utc,
            observation_sha256=observation_sha,
            entitlement_profile_sha256=profile_sha,
            quarantined=quarantined,
        )

    if observation.provider_id != entitlement.provider_id:
        return decision(CausalEligibility.INELIGIBLE, "ENTITLEMENT_PROVIDER_MISMATCH")
    if observation.entitlement_profile_id != entitlement.profile_id:
        return decision(CausalEligibility.INELIGIBLE, "ENTITLEMENT_PROFILE_MISMATCH")

    if observation.provider_published_at is not None:
        published = _parse_timestamp(
            observation.provider_published_at,
            "provider_published_at",
        )
        if published > received:
            return decision(
                CausalEligibility.UNKNOWN,
                "PROVIDER_PUBLISHED_AFTER_RECEIPT",
                quarantined=True,
            )

    if observation.mapping_observed_at is not None:
        mapping_observed = _parse_timestamp(
            observation.mapping_observed_at,
            "mapping_observed_at",
        )
        if mapping_observed > received:
            return decision(
                CausalEligibility.UNKNOWN,
                "IDENTITY_MAPPING_OBSERVED_AFTER_RECEIPT",
                quarantined=True,
            )

    if received > cutoff:
        return decision(CausalEligibility.INELIGIBLE, "RECEIVED_AFTER_DECISION_CUTOFF")

    if entitlement.verification is not EntitlementVerification.VERIFIED_DOCUMENT_BOUND:
        return decision(CausalEligibility.UNKNOWN, "ENTITLEMENT_UNVERIFIED")

    effective_from = _parse_timestamp(entitlement.effective_from, "effective_from")
    if received < effective_from:
        return decision(CausalEligibility.UNKNOWN, "ENTITLEMENT_NOT_YET_EFFECTIVE")
    if entitlement.effective_to is not None:
        effective_to = _parse_timestamp(entitlement.effective_to, "effective_to")
        if received > effective_to:
            return decision(CausalEligibility.UNKNOWN, "ENTITLEMENT_EXPIRED")

    permission = entitlement.permission_for(use)
    if permission is EntitlementPermission.DENIED:
        return decision(CausalEligibility.INELIGIBLE, "ENTITLEMENT_DENIES_USE")
    if permission is EntitlementPermission.UNKNOWN:
        return decision(CausalEligibility.UNKNOWN, "ENTITLEMENT_USE_UNKNOWN")

    latency = observation.latency_class
    if latency is ObservationLatencyClass.UNKNOWN:
        return decision(CausalEligibility.UNKNOWN, "LATENCY_CLASS_UNKNOWN")
    if use is EvidenceUse.LIVE_EXECUTION and latency is not ObservationLatencyClass.LIVE:
        return decision(
            CausalEligibility.INELIGIBLE,
            f"{latency.value}_NOT_LIVE_EXECUTION_EVIDENCE",
        )
    if (
        use is EvidenceUse.FORWARD_ECONOMIC_PROOF
        and latency is ObservationLatencyClass.HISTORICAL
    ):
        return decision(
            CausalEligibility.INELIGIBLE,
            "HISTORICAL_NOT_FORWARD_ECONOMIC_EVIDENCE",
        )

    if latency is ObservationLatencyClass.DELAYED:
        return decision(CausalEligibility.ELIGIBLE, "ELIGIBLE_WITH_DECLARED_DELAY")
    if latency is ObservationLatencyClass.HISTORICAL:
        return decision(CausalEligibility.ELIGIBLE, "ELIGIBLE_HISTORICAL_RESEARCH_ONLY")
    return decision(CausalEligibility.ELIGIBLE, "ELIGIBLE")


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and trimmed")
    return value


def _require_token(value: object, field: str) -> str:
    token = _require_text(value, field)
    if any(character.isspace() for character in token):
        raise ValueError(f"{field} must not contain whitespace")
    return token


def _require_text_tuple(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field} must be tuple")
    seen: set[str] = set()
    for item in value:
        text = _require_text(item, f"{field} item")
        if text in seen:
            raise ValueError(f"{field} must not contain duplicates")
        seen.add(text)
    return value


def _require_sha256(value: object, field: str) -> str:
    digest = _require_text(value, field)
    if len(digest) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return digest


def _parse_timestamp(value: object, field: str) -> datetime:
    text = _require_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware ISO-8601")
    return parsed


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
