"""Fail-closed Betfair deployment/distribution policy metadata.

This module keeps deployment intent separate from technical adapter capability and
integration-channel evidence.  It can detect contradictory product configuration
and bind that configuration to one exact ``BookmakerIntegrationEvidence`` object.

External document references are provenance metadata only.  They do not prove that
Betfair granted a licence, certification, redistribution permission, provider-write
authority, execution authority, or real-money authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_integration_boundary import BookmakerIntegrationEvidence


class BetfairDistributionPolicyError(ValueError):
    """Raised when Betfair deployment/distribution metadata is inconsistent."""


class BetfairUseMode(str, Enum):
    """Declared product deployment/use mode; never provider-granted authority."""

    UNPROVEN = "unproven"
    PERSONAL_PRIVATE = "personal_private"
    SOFTWARE_VENDOR = "software_vendor"
    COMMERCIAL_DATA = "commercial_data"


class VendorCertificationState(str, Enum):
    """Descriptive lifecycle state for Software Vendor certification evidence."""

    NOT_APPLICABLE = "not_applicable"
    UNPROVEN = "unproven"
    IN_PROGRESS = "in_progress"
    DOCUMENTED = "documented"


class DataRedistributionState(str, Enum):
    """Descriptive state for separate data-redistribution documentation."""

    UNPROVEN = "unproven"
    DOCUMENTED = "documented"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairDistributionPolicyError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise BetfairDistributionPolicyError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BetfairDistributionPolicyError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairDistributionPolicyError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _optional_evidence_pair(
    reference: object,
    digest: object,
    *,
    reference_field: str,
    digest_field: str,
) -> tuple[str | None, str | None]:
    if reference is None and digest is None:
        return None, None
    if reference is None or digest is None:
        raise BetfairDistributionPolicyError(
            f"{reference_field} and {digest_field} must be supplied together"
        )
    return _text(reference, reference_field), _sha256(digest, digest_field)


@dataclass(frozen=True, slots=True)
class BetfairDistributionPolicyEvidence:
    """Bounded deployment metadata for one exact Betfair integration evidence.

    ``vendor_certification_state`` and ``data_redistribution_state`` describe what
    external documentation the product operator says is available.  The references
    and digests make that statement deterministic/auditable; this object does not
    authenticate the issuer or grant any external permission.
    """

    integration_evidence_id: str
    use_mode: BetfairUseMode
    distribution_to_other_betfair_customers: bool
    vendor_certification_state: VendorCertificationState
    data_redistribution_state: DataRedistributionState
    checked_at: str
    provider_terms_evidence_ref: str
    provider_terms_evidence_sha256: str
    external_authorization_ref: str | None = None
    external_authorization_sha256: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _sha256(self.integration_evidence_id, "integration_evidence_id")
        if type(self.use_mode) is not BetfairUseMode:
            raise BetfairDistributionPolicyError(
                "use_mode must be an exact BetfairUseMode value"
            )
        if type(self.distribution_to_other_betfair_customers) is not bool:
            raise BetfairDistributionPolicyError(
                "distribution_to_other_betfair_customers must be bool"
            )
        if type(self.vendor_certification_state) is not VendorCertificationState:
            raise BetfairDistributionPolicyError(
                "vendor_certification_state must be an exact VendorCertificationState value"
            )
        if type(self.data_redistribution_state) is not DataRedistributionState:
            raise BetfairDistributionPolicyError(
                "data_redistribution_state must be an exact DataRedistributionState value"
            )
        _timestamp(self.checked_at, "checked_at")
        _text(self.provider_terms_evidence_ref, "provider_terms_evidence_ref")
        _sha256(
            self.provider_terms_evidence_sha256,
            "provider_terms_evidence_sha256",
        )
        external_ref, external_sha = _optional_evidence_pair(
            self.external_authorization_ref,
            self.external_authorization_sha256,
            reference_field="external_authorization_ref",
            digest_field="external_authorization_sha256",
        )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise BetfairDistributionPolicyError(
                "schema_version must be exactly 1"
            )

        positive_external_claim = (
            self.vendor_certification_state is VendorCertificationState.DOCUMENTED
            or self.data_redistribution_state is DataRedistributionState.DOCUMENTED
        )
        if positive_external_claim and external_ref is None:
            raise BetfairDistributionPolicyError(
                "documented external state requires authorization evidence"
            )
        if not positive_external_claim and external_ref is not None:
            raise BetfairDistributionPolicyError(
                "authorization evidence is allowed only for a documented external state"
            )

        if self.use_mode is BetfairUseMode.UNPROVEN:
            if (
                self.distribution_to_other_betfair_customers
                or self.vendor_certification_state is not VendorCertificationState.UNPROVEN
                or self.data_redistribution_state is not DataRedistributionState.UNPROVEN
            ):
                raise BetfairDistributionPolicyError(
                    "unproven use mode cannot claim distribution or external authorization"
                )
        elif self.use_mode is BetfairUseMode.PERSONAL_PRIVATE:
            if (
                self.distribution_to_other_betfair_customers
                or self.vendor_certification_state
                is not VendorCertificationState.NOT_APPLICABLE
                or self.data_redistribution_state
                is not DataRedistributionState.UNPROVEN
            ):
                raise BetfairDistributionPolicyError(
                    "personal-private use cannot claim vendor distribution or data redistribution"
                )
        elif self.use_mode is BetfairUseMode.SOFTWARE_VENDOR:
            if self.vendor_certification_state is VendorCertificationState.NOT_APPLICABLE:
                raise BetfairDistributionPolicyError(
                    "software-vendor mode requires a vendor certification lifecycle state"
                )
            if self.data_redistribution_state is not DataRedistributionState.UNPROVEN:
                raise BetfairDistributionPolicyError(
                    "software-vendor mode cannot substitute for data-redistribution documentation"
                )
            if (
                self.distribution_to_other_betfair_customers
                and self.vendor_certification_state
                is not VendorCertificationState.DOCUMENTED
            ):
                raise BetfairDistributionPolicyError(
                    "distribution to other Betfair customers requires documented vendor certification"
                )
        elif self.use_mode is BetfairUseMode.COMMERCIAL_DATA:
            if self.distribution_to_other_betfair_customers:
                raise BetfairDistributionPolicyError(
                    "commercial-data mode cannot substitute for Software Vendor distribution"
                )
            if self.vendor_certification_state is not VendorCertificationState.NOT_APPLICABLE:
                raise BetfairDistributionPolicyError(
                    "commercial-data mode cannot claim Software Vendor certification"
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
            "checked_at": self.checked_at,
            "data_redistribution_state": self.data_redistribution_state.value,
            "distribution_to_other_betfair_customers": self.distribution_to_other_betfair_customers,
            "external_authorization_ref": self.external_authorization_ref,
            "external_authorization_sha256": self.external_authorization_sha256,
            "integration_evidence_id": self.integration_evidence_id,
            "provider_terms_evidence_ref": self.provider_terms_evidence_ref,
            "provider_terms_evidence_sha256": self.provider_terms_evidence_sha256,
            "schema_version": self.schema_version,
            "use_mode": self.use_mode.value,
            "vendor_certification_state": self.vendor_certification_state.value,
        }

    def verify_integration(self, integration: BookmakerIntegrationEvidence) -> None:
        """Fail closed unless this policy binds the exact integration evidence."""

        if type(integration) is not BookmakerIntegrationEvidence:
            raise BetfairDistributionPolicyError(
                "integration must be an exact BookmakerIntegrationEvidence"
            )
        if integration.evidence_id != self.integration_evidence_id:
            raise BetfairDistributionPolicyError(
                "distribution policy does not match integration evidence identity"
            )

    @property
    def provider_permission_authorized(self) -> bool:
        """External authorization is never self-proved by local metadata."""

        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_authorized(self) -> bool:
        return False


def bind_betfair_distribution_policy(
    integration: BookmakerIntegrationEvidence,
    *,
    use_mode: BetfairUseMode,
    distribution_to_other_betfair_customers: bool,
    vendor_certification_state: VendorCertificationState,
    data_redistribution_state: DataRedistributionState,
    checked_at: str,
    provider_terms_evidence_ref: str,
    provider_terms_evidence_sha256: str,
    external_authorization_ref: str | None = None,
    external_authorization_sha256: str | None = None,
) -> BetfairDistributionPolicyEvidence:
    """Bind fail-closed deployment metadata to exact technical integration evidence."""

    if type(integration) is not BookmakerIntegrationEvidence:
        raise BetfairDistributionPolicyError(
            "integration must be an exact BookmakerIntegrationEvidence"
        )
    policy = BetfairDistributionPolicyEvidence(
        integration_evidence_id=integration.evidence_id,
        use_mode=use_mode,
        distribution_to_other_betfair_customers=distribution_to_other_betfair_customers,
        vendor_certification_state=vendor_certification_state,
        data_redistribution_state=data_redistribution_state,
        checked_at=checked_at,
        provider_terms_evidence_ref=provider_terms_evidence_ref,
        provider_terms_evidence_sha256=provider_terms_evidence_sha256,
        external_authorization_ref=external_authorization_ref,
        external_authorization_sha256=external_authorization_sha256,
    )
    policy.verify_integration(integration)
    return policy
