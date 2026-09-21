"""Fail-closed Betfair application-key purpose/readiness projection.

This module models only non-secret key-purpose and licence-path evidence. It never
accepts or persists an application key, session token, credentials, account secrets,
provider responses, bet instructions, or execution authority.

The projection is deliberately narrower than whole-product readiness:
- DELAYED + READ_ONLY can qualify a development/read-only path, but never real-time
  price authority;
- LIVE + READ_ONLY is blocked because the Live key is not a supported read-only use;
- LIVE + BETTING can only establish key-purpose compatibility when the applicable
  licence path is explicit; it still grants no provider-write or execution authority;
- distributed software requires an explicit SOFTWARE_VENDOR licence path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json


class BetfairAppKeyReadinessError(ValueError):
    """Raised when Betfair key-purpose evidence or a projection is invalid."""


class BetfairApplicationKeyClass(str, Enum):
    DELAYED = "delayed"
    LIVE = "live"


class BetfairUsageIntent(str, Enum):
    READ_ONLY = "read_only"
    BETTING = "betting"


class BetfairDistributionMode(str, Enum):
    PERSONAL = "personal"
    DISTRIBUTED_SOFTWARE = "distributed_software"


class BetfairLicencePath(str, Enum):
    NOT_PROVEN = "not_proven"
    PERSONAL_BETTING = "personal_betting"
    SOFTWARE_VENDOR = "software_vendor"


class BetfairKeyPurposeState(str, Enum):
    READINESS_BLOCKED = "readiness_blocked"
    READ_ONLY_DEVELOPMENT = "read_only_development"
    BETTING_KEY_PURPOSE_COMPATIBLE = "betting_key_purpose_compatible"


def _timestamp(value: object, field: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairAppKeyReadinessError(
            f"{field} must be a non-empty trimmed ISO-8601 string"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BetfairAppKeyReadinessError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairAppKeyReadinessError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise BetfairAppKeyReadinessError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    if any(char not in "0123456789abcdef" for char in value):
        raise BetfairAppKeyReadinessError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return value


@dataclass(frozen=True, slots=True)
class BetfairAppKeyPurposeEvidence:
    """Non-secret evidence describing one configured Betfair key purpose.

    ``evidence_manifest_sha256`` MUST identify a non-secret manifest. Raw application
    keys, session tokens, credentials and provider secrets are intentionally absent from
    this schema and must never be encoded into a durable manifest for this contract.
    """

    key_class: BetfairApplicationKeyClass
    usage_intent: BetfairUsageIntent
    distribution_mode: BetfairDistributionMode
    licence_path: BetfairLicencePath
    observed_at: str
    evidence_manifest_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.key_class) is not BetfairApplicationKeyClass:
            raise BetfairAppKeyReadinessError(
                "key_class must be a BetfairApplicationKeyClass value"
            )
        if type(self.usage_intent) is not BetfairUsageIntent:
            raise BetfairAppKeyReadinessError(
                "usage_intent must be a BetfairUsageIntent value"
            )
        if type(self.distribution_mode) is not BetfairDistributionMode:
            raise BetfairAppKeyReadinessError(
                "distribution_mode must be a BetfairDistributionMode value"
            )
        if type(self.licence_path) is not BetfairLicencePath:
            raise BetfairAppKeyReadinessError(
                "licence_path must be a BetfairLicencePath value"
            )
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.evidence_manifest_sha256, "evidence_manifest_sha256")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise BetfairAppKeyReadinessError("schema_version must be exactly 1")

    def configuration_dict(self) -> dict[str, object]:
        """Return only stable, non-secret semantic configuration fields."""
        return {
            "distribution_mode": self.distribution_mode.value,
            "key_class": self.key_class.value,
            "licence_path": self.licence_path.value,
            "schema_version": self.schema_version,
            "usage_intent": self.usage_intent.value,
        }

    @property
    def configuration_id(self) -> str:
        encoded = json.dumps(
            self.configuration_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            **self.configuration_dict(),
            "configuration_id": self.configuration_id,
            "evidence_manifest_sha256": self.evidence_manifest_sha256,
            "observed_at": self.observed_at,
        }

    @property
    def evidence_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairAppKeyPurposeAssessment:
    """Deterministic projection over one exact non-secret purpose evidence object.

    Positive fields are derived properties rather than constructor-controlled booleans.
    This assessment can qualify only this narrow key-purpose gate. It never grants
    real-time price authority, provider-write authority, real-money execution authority,
    or whole-product readiness.
    """

    evidence: BetfairAppKeyPurposeEvidence

    def __post_init__(self) -> None:
        if type(self.evidence) is not BetfairAppKeyPurposeEvidence:
            raise BetfairAppKeyReadinessError(
                "evidence must be BetfairAppKeyPurposeEvidence"
            )

    @property
    def state(self) -> BetfairKeyPurposeState:
        if not self._licence_path_satisfied:
            return BetfairKeyPurposeState.READINESS_BLOCKED
        if (
            self.evidence.key_class is BetfairApplicationKeyClass.LIVE
            and self.evidence.usage_intent is BetfairUsageIntent.READ_ONLY
        ):
            return BetfairKeyPurposeState.READINESS_BLOCKED
        if self.evidence.key_class is BetfairApplicationKeyClass.DELAYED:
            if self.evidence.usage_intent is BetfairUsageIntent.READ_ONLY:
                return BetfairKeyPurposeState.READ_ONLY_DEVELOPMENT
            return BetfairKeyPurposeState.READINESS_BLOCKED
        if self.evidence.usage_intent is BetfairUsageIntent.BETTING:
            return BetfairKeyPurposeState.BETTING_KEY_PURPOSE_COMPATIBLE
        return BetfairKeyPurposeState.READINESS_BLOCKED

    @property
    def key_purpose_gate_passed(self) -> bool:
        return self.state is not BetfairKeyPurposeState.READINESS_BLOCKED

    @property
    def development_read_only_supported(self) -> bool:
        return self.state is BetfairKeyPurposeState.READ_ONLY_DEVELOPMENT

    @property
    def live_betting_key_purpose_compatible(self) -> bool:
        return self.state is BetfairKeyPurposeState.BETTING_KEY_PURPOSE_COMPATIBLE

    @property
    def realtime_price_authority_granted(self) -> bool:
        """Key-purpose evidence alone can never prove current real-time price truth."""
        return False

    @property
    def provider_write_authority_granted(self) -> bool:
        return False

    @property
    def execution_authority_granted(self) -> bool:
        return False

    @property
    def whole_product_readiness_granted(self) -> bool:
        return False

    @property
    def production_exchange_endpoint(self) -> bool:
        """Betfair DELAYED and LIVE keys both address the production Exchange APIs."""
        return True

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self._licence_path_satisfied:
            if self.evidence.distribution_mode is BetfairDistributionMode.DISTRIBUTED_SOFTWARE:
                reasons.append("SOFTWARE_VENDOR_LICENCE_REQUIRED")
            else:
                reasons.append("PERSONAL_BETTING_LICENCE_REQUIRED")
        if (
            self.evidence.key_class is BetfairApplicationKeyClass.LIVE
            and self.evidence.usage_intent is BetfairUsageIntent.READ_ONLY
        ):
            reasons.append("LIVE_KEY_READ_ONLY_PURPOSE_CONFLICT")
        if self.evidence.key_class is BetfairApplicationKeyClass.DELAYED:
            reasons.append("DELAYED_KEY_HAS_NO_REALTIME_PRICE_AUTHORITY")
            if self.evidence.usage_intent is BetfairUsageIntent.BETTING:
                reasons.append("DELAYED_KEY_CANNOT_QUALIFY_LIVE_BETTING_READINESS")
        reasons.append("KEY_PURPOSE_EVIDENCE_GRANTS_NO_EXECUTION_AUTHORITY")
        return tuple(reasons)

    @property
    def _licence_path_satisfied(self) -> bool:
        if self.evidence.distribution_mode is BetfairDistributionMode.DISTRIBUTED_SOFTWARE:
            return self.evidence.licence_path is BetfairLicencePath.SOFTWARE_VENDOR
        if (
            self.evidence.key_class is BetfairApplicationKeyClass.LIVE
            and self.evidence.usage_intent is BetfairUsageIntent.BETTING
        ):
            return self.evidence.licence_path is BetfairLicencePath.PERSONAL_BETTING
        return self.evidence.licence_path in {
            BetfairLicencePath.NOT_PROVEN,
            BetfairLicencePath.PERSONAL_BETTING,
        }

    def verify_current(self, current: BetfairAppKeyPurposeEvidence) -> None:
        """Fail closed when key class, purpose, distribution or licence scope drifted."""
        if type(current) is not BetfairAppKeyPurposeEvidence:
            raise BetfairAppKeyReadinessError(
                "current must be BetfairAppKeyPurposeEvidence"
            )
        if current.configuration_id != self.evidence.configuration_id:
            raise BetfairAppKeyReadinessError(
                "Betfair app-key purpose configuration changed; readiness must be re-resolved"
            )

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "configuration_id": self.evidence.configuration_id,
            "development_read_only_supported": self.development_read_only_supported,
            "evidence_id": self.evidence.evidence_id,
            "execution_authority_granted": self.execution_authority_granted,
            "key_purpose_gate_passed": self.key_purpose_gate_passed,
            "live_betting_key_purpose_compatible": self.live_betting_key_purpose_compatible,
            "production_exchange_endpoint": self.production_exchange_endpoint,
            "provider_write_authority_granted": self.provider_write_authority_granted,
            "realtime_price_authority_granted": self.realtime_price_authority_granted,
            "reasons": list(self.reasons),
            "state": self.state.value,
            "whole_product_readiness_granted": self.whole_product_readiness_granted,
        }


def assess_betfair_app_key_purpose(
    evidence: BetfairAppKeyPurposeEvidence,
) -> BetfairAppKeyPurposeAssessment:
    """Project non-secret key-purpose evidence into a bounded readiness assessment."""
    return BetfairAppKeyPurposeAssessment(evidence=evidence)
