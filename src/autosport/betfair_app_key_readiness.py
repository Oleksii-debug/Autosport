"""Fail-closed Betfair application-key purpose/readiness composition.

This module is a dependent consumer of the canonical authenticated Betfair
application-key class issued by ``betfair_capability_freshness``. It does not define a
second DELAYED/LIVE vocabulary and never accepts raw application keys, session tokens or
credentials.

The result is deliberately narrower than whole-product readiness:
- authenticated DELAYED + READ_ONLY can qualify only a development/read-only path;
- authenticated LIVE + READ_ONLY is blocked;
- authenticated LIVE + BETTING is only key-purpose compatible and grants no provider
  write, real-time price, execution, licence, or whole-product authority;
- distributed software remains blocked until a separate product-owned Software Vendor
  licence authority is composed. A caller declaration cannot mint that authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json

from .betfair_capability_freshness import (
    BetfairApplicationKeyClass,
    BetfairCapabilityFreshnessEvidence,
    BetfairProviderEnvironment,
)


class BetfairAppKeyReadinessError(ValueError):
    """Raised when key-purpose configuration or upstream evidence is invalid/stale."""


class BetfairUsageIntent(str, Enum):
    READ_ONLY = "read_only"
    BETTING = "betting"


class BetfairDistributionMode(str, Enum):
    PERSONAL = "personal"
    DISTRIBUTED_SOFTWARE = "distributed_software"


class BetfairKeyPurposeState(str, Enum):
    READINESS_BLOCKED = "readiness_blocked"
    READ_ONLY_DEVELOPMENT = "read_only_development"
    BETTING_KEY_PURPOSE_COMPATIBLE = "betting_key_purpose_compatible"


@dataclass(frozen=True, slots=True)
class BetfairAppKeyPurposeConfig:
    """Non-secret operator/product intent used by the bounded purpose gate."""

    usage_intent: BetfairUsageIntent
    distribution_mode: BetfairDistributionMode
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.usage_intent) is not BetfairUsageIntent:
            raise BetfairAppKeyReadinessError(
                "usage_intent must be a BetfairUsageIntent value"
            )
        if type(self.distribution_mode) is not BetfairDistributionMode:
            raise BetfairAppKeyReadinessError(
                "distribution_mode must be a BetfairDistributionMode value"
            )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise BetfairAppKeyReadinessError("schema_version must be exactly 1")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "distribution_mode": self.distribution_mode.value,
            "schema_version": self.schema_version,
            "usage_intent": self.usage_intent.value,
        }

    @property
    def configuration_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairAppKeyPurposeAssessment:
    """Ephemeral purpose assessment over exact product-issued #825 evidence.

    The upstream freshness object retains authority for authenticated key-class origin
    and credential/session continuity. Every public projection re-checks that authority;
    a serialized/reconstructed source object or a source invalidated by its upstream
    authority cannot retain this assessment's positive state after restart.
    """

    source: BetfairCapabilityFreshnessEvidence
    config: BetfairAppKeyPurposeConfig

    def __post_init__(self) -> None:
        if type(self.source) is not BetfairCapabilityFreshnessEvidence:
            raise BetfairAppKeyReadinessError(
                "source must be exact BetfairCapabilityFreshnessEvidence"
            )
        if type(self.config) is not BetfairAppKeyPurposeConfig:
            raise BetfairAppKeyReadinessError(
                "config must be exact BetfairAppKeyPurposeConfig"
            )
        self._assert_source_current()

    def _assert_source_current(self) -> None:
        try:
            self.source._assert_product_issued()
        except Exception as exc:
            raise BetfairAppKeyReadinessError(
                "Betfair application-key class lacks current product-issued authority"
            ) from exc
        if (
            self.source.environment
            is not BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE
        ):
            raise BetfairAppKeyReadinessError(
                "Betfair application-key purpose requires the canonical production Exchange environment"
            )

    @property
    def _authenticated_key_class(self) -> BetfairApplicationKeyClass | None:
        self._assert_source_current()
        if self.source.authenticated_context_sha256 is None:
            return None
        key_class = self.source.application_key_class
        if key_class is BetfairApplicationKeyClass.UNKNOWN:
            return None
        return key_class

    @property
    def state(self) -> BetfairKeyPurposeState:
        key_class = self._authenticated_key_class
        if self.config.distribution_mode is BetfairDistributionMode.DISTRIBUTED_SOFTWARE:
            return BetfairKeyPurposeState.READINESS_BLOCKED
        if key_class is None:
            return BetfairKeyPurposeState.READINESS_BLOCKED
        if key_class is BetfairApplicationKeyClass.LIVE:
            if self.config.usage_intent is BetfairUsageIntent.READ_ONLY:
                return BetfairKeyPurposeState.READINESS_BLOCKED
            return BetfairKeyPurposeState.BETTING_KEY_PURPOSE_COMPATIBLE
        if self.config.usage_intent is BetfairUsageIntent.READ_ONLY:
            return BetfairKeyPurposeState.READ_ONLY_DEVELOPMENT
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
        return False

    @property
    def provider_write_authority_granted(self) -> bool:
        return False

    @property
    def execution_authority_granted(self) -> bool:
        return False

    @property
    def licence_authority_granted(self) -> bool:
        return False

    @property
    def whole_product_readiness_granted(self) -> bool:
        return False

    @property
    def reasons(self) -> tuple[str, ...]:
        key_class = self._authenticated_key_class
        reasons: list[str] = []
        if self.config.distribution_mode is BetfairDistributionMode.DISTRIBUTED_SOFTWARE:
            reasons.append("SOFTWARE_VENDOR_LICENCE_AUTHORITY_REQUIRED")
        if key_class is None:
            reasons.append("AUTHENTICATED_APPLICATION_KEY_CLASS_REQUIRED")
        elif (
            key_class is BetfairApplicationKeyClass.LIVE
            and self.config.usage_intent is BetfairUsageIntent.READ_ONLY
        ):
            reasons.append("LIVE_KEY_READ_ONLY_PURPOSE_CONFLICT")
        elif key_class is BetfairApplicationKeyClass.DELAYED:
            reasons.append("DELAYED_KEY_HAS_NO_REALTIME_PRICE_AUTHORITY")
            if self.config.usage_intent is BetfairUsageIntent.BETTING:
                reasons.append("DELAYED_KEY_CANNOT_QUALIFY_LIVE_BETTING_READINESS")
        reasons.append("KEY_PURPOSE_GATE_GRANTS_NO_EXECUTION_OR_LICENCE_AUTHORITY")
        return tuple(reasons)

    def assert_current_config(self, current: BetfairAppKeyPurposeConfig) -> None:
        """Re-check upstream authority and fail closed on purpose/distribution drift."""
        self._assert_source_current()
        if type(current) is not BetfairAppKeyPurposeConfig:
            raise BetfairAppKeyReadinessError(
                "current must be exact BetfairAppKeyPurposeConfig"
            )
        if current.configuration_id != self.config.configuration_id:
            raise BetfairAppKeyReadinessError(
                "Betfair app-key purpose configuration changed; readiness must be re-resolved"
            )

    def to_canonical_dict(self) -> dict[str, object]:
        """Return non-authoritative diagnostic projection; source authority is not serializable."""
        return {
            "configuration_id": self.config.configuration_id,
            "development_read_only_supported": self.development_read_only_supported,
            "execution_authority_granted": self.execution_authority_granted,
            "key_purpose_gate_passed": self.key_purpose_gate_passed,
            "licence_authority_granted": self.licence_authority_granted,
            "live_betting_key_purpose_compatible": self.live_betting_key_purpose_compatible,
            "provider_write_authority_granted": self.provider_write_authority_granted,
            "realtime_price_authority_granted": self.realtime_price_authority_granted,
            "reasons": list(self.reasons),
            "source_evidence_id": self.source.evidence_id,
            "state": self.state.value,
            "whole_product_readiness_granted": self.whole_product_readiness_granted,
        }


def assess_betfair_app_key_purpose(
    source: BetfairCapabilityFreshnessEvidence,
    config: BetfairAppKeyPurposeConfig,
) -> BetfairAppKeyPurposeAssessment:
    """Compose canonical authenticated key-class truth with non-secret product intent."""
    return BetfairAppKeyPurposeAssessment(source=source, config=config)
