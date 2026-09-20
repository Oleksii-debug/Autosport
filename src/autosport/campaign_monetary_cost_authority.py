from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostEvidence,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
)
from .campaign_economic_authority import CanonicalMembershipRef


SCHEMA_VERSION = 3
RECEIPT_FAMILY = "autosport.monetary.receipt.v1"
ALLOCATION_FAMILY = "autosport.monetary.allocation.v1"
CURRENCY_FAMILY = "autosport.monetary.campaign-currency.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class MonetaryCostAuthorityError(ValueError):
    """Raised when monetary evidence is invalid or not authoritative."""


class MonetaryCostAuthorityIntegrityError(MonetaryCostAuthorityError):
    """Raised when persisted monetary authority evidence fails integrity checks."""


class MonetarySourceClass(StrEnum):
    PROVIDER_BILLING = "PROVIDER_BILLING"
    COMPUTE_BILLING = "COMPUTE_BILLING"
    EXECUTION_RECEIPT = "EXECUTION_RECEIPT"
    FIXED_CAMPAIGN_ADMIN = "FIXED_CAMPAIGN_ADMIN"


class MonetaryEvidenceQuality(StrEnum):
    INCURRED_ACTUAL = "INCURRED_ACTUAL"
    ESTIMATE = "ESTIMATE"
    SIMULATED = "SIMULATED"
    UNVERIFIED = "UNVERIFIED"


class MonetaryResolverFamily(StrEnum):
    PROVIDER_ACCOUNT_BILLING = "PROVIDER_ACCOUNT_BILLING"
    COMPUTE_BILLING = "COMPUTE_BILLING"
    EXECUTION_SETTLEMENT = "EXECUTION_SETTLEMENT"
    OWNER_FIXED_EXPENSE = "OWNER_FIXED_EXPENSE"
    OWNER_CAMPAIGN_CURRENCY = "OWNER_CAMPAIGN_CURRENCY"


@dataclass(frozen=True, slots=True)
class MoneyAmount:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        _finite_decimal(self.amount, "amount")
        if self.amount < 0:
            raise MonetaryCostAuthorityError("money amount cannot be negative")
        if not isinstance(self.currency, str) or _CURRENCY_RE.fullmatch(self.currency) is None:
            raise MonetaryCostAuthorityError(
                "currency must be an uppercase three-letter ISO-style code"
            )

    def to_dict(self) -> dict[str, str]:
        return {"amount": _decimal_text(self.amount), "currency": self.currency}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MoneyAmount":
        _exact_keys(raw, {"amount", "currency"}, "MoneyAmount")
        return cls(
            amount=_parse_decimal(raw["amount"], "amount"),
            currency=_string(raw["currency"], "currency"),
        )


@dataclass(frozen=True, slots=True)
class CampaignCurrencyEvidence:
    campaign_sha256: str
    currency: str
    source_authority: str
    source_evidence_id: str
    source_sha256: str
    observed_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        _sha256(self.campaign_sha256, "campaign_sha256")
        if not isinstance(self.currency, str) or _CURRENCY_RE.fullmatch(self.currency) is None:
            raise MonetaryCostAuthorityError(
                "campaign currency must be an uppercase three-letter code"
            )
        _text(self.source_authority, "source_authority")
        _text(self.source_evidence_id, "source_evidence_id")
        _sha256(self.source_sha256, "source_sha256")
        _utc(self.observed_at, "observed_at")
        _utc(self.available_at, "available_at")
        if self.observed_at > self.available_at:
            raise MonetaryCostAuthorityError("observed_at cannot be after available_at")

    @property
    def evidence_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.evidence_id

    @property
    def ref(self) -> CostSourceRef:
        return CostSourceRef(
            family=CURRENCY_FAMILY,
            evidence_id=self.evidence_id,
            sha256=self.record_sha256,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "campaign_sha256": self.campaign_sha256,
            "currency": self.currency,
            "source_authority": self.source_authority,
            "source_evidence_id": self.source_evidence_id,
            "source_sha256": self.source_sha256,
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["evidence_id"] = self.evidence_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CampaignCurrencyEvidence":
        expected = {
            "schema_version",
            "campaign_sha256",
            "currency",
            "source_authority",
            "source_evidence_id",
            "source_sha256",
            "observed_at",
            "available_at",
            "evidence_id",
            "record_sha256",
        }
        _exact_keys(raw, expected, "CampaignCurrencyEvidence")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryCostAuthorityIntegrityError(
                "unsupported campaign currency evidence schema"
            )
        item = cls(
            campaign_sha256=_string(raw["campaign_sha256"], "campaign_sha256"),
            currency=_string(raw["currency"], "currency"),
            source_authority=_string(raw["source_authority"], "source_authority"),
            source_evidence_id=_string(raw["source_evidence_id"], "source_evidence_id"),
            source_sha256=_string(raw["source_sha256"], "source_sha256"),
            observed_at=_parse_datetime(raw["observed_at"], "observed_at"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
        )
        if _string(raw["evidence_id"], "evidence_id") != item.evidence_id:
            raise MonetaryCostAuthorityIntegrityError("campaign currency evidence id mismatch")
        if _string(raw["record_sha256"], "record_sha256") != item.record_sha256:
            raise MonetaryCostAuthorityIntegrityError(
                "campaign currency record digest mismatch"
            )
        return item


@dataclass(frozen=True, slots=True)
class MonetaryReceipt:
    cost_class: CostClass
    source_class: MonetarySourceClass
    source_authority: str
    source_evidence_id: str
    source_sha256: str
    provenance_sha256: str
    money: MoneyAmount
    incurred_start: datetime
    incurred_end: datetime
    observed_at: datetime
    available_at: datetime
    treatment: CostTreatment
    shared_source: bool
    campaign_sha256: str | None
    memberships: tuple[CanonicalMembershipRef, ...]
    quality: MonetaryEvidenceQuality = MonetaryEvidenceQuality.INCURRED_ACTUAL
    covered_start: datetime | None = None
    covered_end: datetime | None = None
    upstream_refs: tuple[CostSourceRef, ...] = ()
    supersedes_receipt_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.source_authority, "source_authority")
        _text(self.source_evidence_id, "source_evidence_id")
        _sha256(self.source_sha256, "source_sha256")
        _sha256(self.provenance_sha256, "provenance_sha256")
        if not isinstance(self.quality, MonetaryEvidenceQuality):
            raise MonetaryCostAuthorityError("quality must be MonetaryEvidenceQuality")
        for label, value in (
            ("incurred_start", self.incurred_start),
            ("incurred_end", self.incurred_end),
            ("observed_at", self.observed_at),
            ("available_at", self.available_at),
        ):
            _utc(value, label)
        if self.incurred_end < self.incurred_start:
            raise MonetaryCostAuthorityError("incurred_end cannot precede incurred_start")
        if self.observed_at > self.available_at:
            raise MonetaryCostAuthorityError("observed_at cannot be after available_at")
        if self.incurred_end > self.available_at:
            raise MonetaryCostAuthorityError("incurred cost cannot be available before it occurred")

        covered_start = self.incurred_start if self.covered_start is None else self.covered_start
        covered_end = self.incurred_end if self.covered_end is None else self.covered_end
        _utc(covered_start, "covered_start")
        _utc(covered_end, "covered_end")
        if covered_end < covered_start:
            raise MonetaryCostAuthorityError("covered_end cannot precede covered_start")
        object.__setattr__(self, "covered_start", covered_start)
        object.__setattr__(self, "covered_end", covered_end)

        if self.quality is MonetaryEvidenceQuality.INCURRED_ACTUAL:
            if self.treatment is CostTreatment.INFORMATIONAL:
                raise MonetaryCostAuthorityError(
                    "incurred monetary evidence cannot be informational-only"
                )
        elif self.treatment is not CostTreatment.INFORMATIONAL:
            raise MonetaryCostAuthorityError(
                "estimate/simulated/unverified money must remain informational-only"
            )

        _validate_source_class(self.cost_class, self.source_class)
        _sorted_unique(self.memberships, "memberships")
        _sorted_unique(self.upstream_refs, "upstream_refs")
        _sorted_unique(self.supersedes_receipt_ids, "supersedes_receipt_ids")
        for receipt_id in self.supersedes_receipt_ids:
            _sha256(receipt_id, "superseded receipt id")

        if self.shared_source:
            if self.campaign_sha256 is not None or self.memberships:
                raise MonetaryCostAuthorityError(
                    "shared receipt must defer campaign membership to an allocation plan"
                )
        else:
            if self.campaign_sha256 is None:
                raise MonetaryCostAuthorityError(
                    "exclusive receipt requires campaign_sha256"
                )
            _sha256(self.campaign_sha256, "campaign_sha256")
            if not self.memberships:
                raise MonetaryCostAuthorityError(
                    "exclusive receipt requires concrete campaign memberships"
                )

    @property
    def receipt_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.receipt_id

    @property
    def ref(self) -> CostSourceRef:
        return CostSourceRef(
            family=RECEIPT_FAMILY,
            evidence_id=self.receipt_id,
            sha256=self.record_sha256,
        )

    def payload(self) -> dict[str, Any]:
        assert self.covered_start is not None and self.covered_end is not None
        return {
            "schema_version": SCHEMA_VERSION,
            "cost_class": self.cost_class.value,
            "source_class": self.source_class.value,
            "source_authority": self.source_authority,
            "source_evidence_id": self.source_evidence_id,
            "source_sha256": self.source_sha256,
            "provenance_sha256": self.provenance_sha256,
            "money": self.money.to_dict(),
            "incurred_start": _datetime_text(self.incurred_start),
            "incurred_end": _datetime_text(self.incurred_end),
            "covered_start": _datetime_text(self.covered_start),
            "covered_end": _datetime_text(self.covered_end),
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
            "treatment": self.treatment.value,
            "shared_source": self.shared_source,
            "campaign_sha256": self.campaign_sha256,
            "memberships": [_membership_dict(item) for item in self.memberships],
            "quality": self.quality.value,
            "upstream_refs": [item.to_dict() for item in self.upstream_refs],
            "supersedes_receipt_ids": list(self.supersedes_receipt_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["receipt_id"] = self.receipt_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MonetaryReceipt":
        expected = {
            "schema_version", "cost_class", "source_class", "source_authority",
            "source_evidence_id", "source_sha256", "provenance_sha256", "money",
            "incurred_start", "incurred_end", "covered_start", "covered_end",
            "observed_at", "available_at", "treatment", "shared_source",
            "campaign_sha256", "memberships", "quality", "upstream_refs",
            "supersedes_receipt_ids", "receipt_id", "record_sha256",
        }
        _exact_keys(raw, expected, "MonetaryReceipt")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryCostAuthorityIntegrityError("unsupported monetary receipt schema")
        item = cls(
            cost_class=CostClass(_string(raw["cost_class"], "cost_class")),
            source_class=MonetarySourceClass(_string(raw["source_class"], "source_class")),
            source_authority=_string(raw["source_authority"], "source_authority"),
            source_evidence_id=_string(raw["source_evidence_id"], "source_evidence_id"),
            source_sha256=_string(raw["source_sha256"], "source_sha256"),
            provenance_sha256=_string(raw["provenance_sha256"], "provenance_sha256"),
            money=MoneyAmount.from_dict(_mapping(raw["money"], "money")),
            incurred_start=_parse_datetime(raw["incurred_start"], "incurred_start"),
            incurred_end=_parse_datetime(raw["incurred_end"], "incurred_end"),
            observed_at=_parse_datetime(raw["observed_at"], "observed_at"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
            treatment=CostTreatment(_string(raw["treatment"], "treatment")),
            shared_source=_bool(raw["shared_source"], "shared_source"),
            campaign_sha256=_optional_string(raw["campaign_sha256"], "campaign_sha256"),
            memberships=tuple(
                _membership_from_dict(_mapping(value, "membership"))
                for value in _list(raw["memberships"], "memberships")
            ),
            quality=MonetaryEvidenceQuality(_string(raw["quality"], "quality")),
            covered_start=_parse_datetime(raw["covered_start"], "covered_start"),
            covered_end=_parse_datetime(raw["covered_end"], "covered_end"),
            upstream_refs=tuple(
                CostSourceRef.from_dict(_mapping(value, "upstream_ref"))
                for value in _list(raw["upstream_refs"], "upstream_refs")
            ),
            supersedes_receipt_ids=tuple(
                _string(value, "superseded receipt id")
                for value in _list(raw["supersedes_receipt_ids"], "supersedes_receipt_ids")
            ),
        )
        if _string(raw["receipt_id"], "receipt_id") != item.receipt_id:
            raise MonetaryCostAuthorityIntegrityError("monetary receipt id mismatch")
        if _string(raw["record_sha256"], "record_sha256") != item.record_sha256:
            raise MonetaryCostAuthorityIntegrityError("monetary receipt digest mismatch")
        return item


@dataclass(frozen=True, order=True, slots=True)
class AllocationTarget:
    campaign_sha256: str
    memberships: tuple[CanonicalMembershipRef, ...]
    money: MoneyAmount

    def __post_init__(self) -> None:
        _sha256(self.campaign_sha256, "campaign_sha256")
        if not self.memberships:
            raise MonetaryCostAuthorityError("allocation target requires memberships")
        _sorted_unique(self.memberships, "memberships")

    @property
    def target_key(self) -> tuple[str, tuple[CanonicalMembershipRef, ...]]:
        return self.campaign_sha256, self.memberships

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_sha256": self.campaign_sha256,
            "memberships": [_membership_dict(item) for item in self.memberships],
            "money": self.money.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AllocationTarget":
        _exact_keys(raw, {"campaign_sha256", "memberships", "money"}, "AllocationTarget")
        return cls(
            campaign_sha256=_string(raw["campaign_sha256"], "campaign_sha256"),
            memberships=tuple(
                _membership_from_dict(_mapping(value, "membership"))
                for value in _list(raw["memberships"], "memberships")
            ),
            money=MoneyAmount.from_dict(_mapping(raw["money"], "money")),
        )


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    receipt_id: str
    receipt_sha256: str
    targets: tuple[AllocationTarget, ...]
    observed_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        _sha256(self.receipt_id, "receipt_id")
        _sha256(self.receipt_sha256, "receipt_sha256")
        if not self.targets:
            raise MonetaryCostAuthorityError("allocation plan requires at least one target")
        _sorted_unique(self.targets, "allocation targets", key=lambda value: value.target_key)
        _utc(self.observed_at, "observed_at")
        _utc(self.available_at, "available_at")
        if self.observed_at > self.available_at:
            raise MonetaryCostAuthorityError("allocation observed_at cannot be after available_at")

    @property
    def allocation_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.allocation_id

    @property
    def ref(self) -> CostSourceRef:
        return CostSourceRef(
            family=ALLOCATION_FAMILY,
            evidence_id=self.allocation_id,
            sha256=self.record_sha256,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "receipt_sha256": self.receipt_sha256,
            "targets": [item.to_dict() for item in self.targets],
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["allocation_id"] = self.allocation_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AllocationPlan":
        expected = {
            "schema_version", "receipt_id", "receipt_sha256", "targets",
            "observed_at", "available_at", "allocation_id", "record_sha256",
        }
        _exact_keys(raw, expected, "AllocationPlan")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryCostAuthorityIntegrityError("unsupported allocation schema")
        item = cls(
            receipt_id=_string(raw["receipt_id"], "receipt_id"),
            receipt_sha256=_string(raw["receipt_sha256"], "receipt_sha256"),
            targets=tuple(
                AllocationTarget.from_dict(_mapping(value, "allocation target"))
                for value in _list(raw["targets"], "targets")
            ),
            observed_at=_parse_datetime(raw["observed_at"], "observed_at"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
        )
        if _string(raw["allocation_id"], "allocation_id") != item.allocation_id:
            raise MonetaryCostAuthorityIntegrityError("allocation id mismatch")
        if _string(raw["record_sha256"], "record_sha256") != item.record_sha256:
            raise MonetaryCostAuthorityIntegrityError("allocation digest mismatch")
        return item


_RESOLUTION_TOKEN = object()


class ResolvedMonetaryReceipt:
    """Opaque capability minted only by package-owned source resolvers.

    There is deliberately no public constructor/factory. Future canonical provider,
    compute, settlement, or owner-admin resolver modules must validate their native
    source authority first and then use the package-private issuer below. Raw strings,
    digests, JSON, or MonetaryReceipt objects are never sufficient for admission.
    """

    __slots__ = ("receipt", "resolver_family", "resolver_evidence_id", "resolver_sha256")

    def __init__(
        self,
        receipt: MonetaryReceipt,
        resolver_family: MonetaryResolverFamily,
        resolver_evidence_id: str,
        resolver_sha256: str,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _RESOLUTION_TOKEN:
            raise TypeError("ResolvedMonetaryReceipt cannot be caller-constructed")
        self.receipt = receipt
        self.resolver_family = resolver_family
        self.resolver_evidence_id = resolver_evidence_id
        self.resolver_sha256 = resolver_sha256


class ResolvedCampaignCurrency:
    """Opaque campaign-currency capability from a package-owned resolver."""

    __slots__ = ("evidence", "resolver_family", "resolver_evidence_id", "resolver_sha256")

    def __init__(
        self,
        evidence: CampaignCurrencyEvidence,
        resolver_family: MonetaryResolverFamily,
        resolver_evidence_id: str,
        resolver_sha256: str,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _RESOLUTION_TOKEN:
            raise TypeError("ResolvedCampaignCurrency cannot be caller-constructed")
        self.evidence = evidence
        self.resolver_family = resolver_family
        self.resolver_evidence_id = resolver_evidence_id
        self.resolver_sha256 = resolver_sha256


def _issue_resolved_receipt(
    *,
    receipt: MonetaryReceipt,
    resolver_family: MonetaryResolverFamily,
    resolver_evidence_id: str,
    resolver_sha256: str,
) -> ResolvedMonetaryReceipt:
    """Package-private issuer for future canonical source-resolver modules.

    This is intentionally underscored and is not an application/domain API. Adding a
    production call site is a source change that must itself prove the native upstream
    authority before invoking this issuer.
    """

    if type(receipt) is not MonetaryReceipt:
        raise MonetaryCostAuthorityError("resolver receipt has invalid type")
    if not isinstance(resolver_family, MonetaryResolverFamily):
        raise MonetaryCostAuthorityError("resolver_family is invalid")
    _text(resolver_evidence_id, "resolver_evidence_id")
    _sha256(resolver_sha256, "resolver_sha256")
    _validate_resolver_family(receipt.cost_class, resolver_family)
    return ResolvedMonetaryReceipt(
        receipt,
        resolver_family,
        resolver_evidence_id,
        resolver_sha256,
        _token=_RESOLUTION_TOKEN,
    )


def _issue_resolved_currency(
    *,
    evidence: CampaignCurrencyEvidence,
    resolver_evidence_id: str,
    resolver_sha256: str,
) -> ResolvedCampaignCurrency:
    """Package-private issuer for a future canonical owner/account currency resolver."""

    if type(evidence) is not CampaignCurrencyEvidence:
        raise MonetaryCostAuthorityError("resolver currency evidence has invalid type")
    _text(resolver_evidence_id, "resolver_evidence_id")
    _sha256(resolver_sha256, "resolver_sha256")
    return ResolvedCampaignCurrency(
        evidence,
        MonetaryResolverFamily.OWNER_CAMPAIGN_CURRENCY,
        resolver_evidence_id,
        resolver_sha256,
        _token=_RESOLUTION_TOKEN,
    )


class CampaignMonetaryCostAuthority:
    """Durable admitted-money authority consumed by #645 campaign economics.

    Raw evidence may be stored for audit, but only an opaque Resolved* capability can
    create an admission binding. Current main has no concrete provider/account billing,
    compute-billing, execution-settlement, or owner-admin resolver that calls the private
    issuers above, so this class intentionally cannot turn arbitrary caller input into
    COMPLETE_NET_ECONOMICS. Future resolvers must land as separately reviewed source
    adapters and can then feed this stable admission/persistence boundary.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.receipts_dir = self.root / "receipts"
        self.allocations_dir = self.root / "allocations"
        self.currency_dir = self.root / "campaign-currency"
        self.admissions_dir = self.root / "admissions"
        self.source_index_dir = self.root / "source-index"
        self.allocation_index_dir = self.root / "allocation-by-receipt"
        self.correction_index_dir = self.root / "correction-by-receipt"
        self.campaign_currency_index_dir = self.root / "currency-by-campaign"

    def store_receipt_candidate(self, receipt: MonetaryReceipt) -> CostSourceRef:
        if type(receipt) is not MonetaryReceipt:
            raise MonetaryCostAuthorityError("receipt candidate has invalid type")
        self._write_record(self.receipts_dir / f"{receipt.receipt_id}.json", receipt.to_dict())
        return receipt.ref

    def store_currency_candidate(self, evidence: CampaignCurrencyEvidence) -> CostSourceRef:
        if type(evidence) is not CampaignCurrencyEvidence:
            raise MonetaryCostAuthorityError("currency candidate has invalid type")
        self._write_record(self.currency_dir / f"{evidence.evidence_id}.json", evidence.to_dict())
        return evidence.ref

    def publish_receipt(self, resolution: ResolvedMonetaryReceipt) -> CostSourceRef:
        if type(resolution) is not ResolvedMonetaryReceipt:
            raise MonetaryCostAuthorityError(
                "authoritative receipt publication requires ResolvedMonetaryReceipt capability"
            )
        receipt = resolution.receipt
        _validate_resolver_family(receipt.cost_class, resolution.resolver_family)
        _text(resolution.resolver_evidence_id, "resolver_evidence_id")
        _sha256(resolution.resolver_sha256, "resolver_sha256")
        if receipt.quality is not MonetaryEvidenceQuality.INCURRED_ACTUAL:
            raise MonetaryCostAuthorityError(
                "non-incurred resolver evidence cannot be admitted as monetary truth"
            )
        if receipt.shared_source and receipt.supersedes_receipt_ids:
            raise MonetaryCostAuthorityError(
                "shared receipt corrections require a future conserved allocation lineage"
            )
        for prior_id in receipt.supersedes_receipt_ids:
            prior = self._load_receipt(prior_id)
            self._require_receipt_admitted(prior)
            if prior.cost_class is not receipt.cost_class:
                raise MonetaryCostAuthorityError("receipt correction cannot cross cost class")
            if prior.source_class is not receipt.source_class:
                raise MonetaryCostAuthorityError("receipt correction cannot cross source class")
            if prior.money.currency != receipt.money.currency:
                raise MonetaryCostAuthorityError("receipt correction cannot change currency")
            if prior.shared_source != receipt.shared_source:
                raise MonetaryCostAuthorityError("receipt correction cannot change sharing mode")
            if prior.campaign_sha256 != receipt.campaign_sha256 or prior.memberships != receipt.memberships:
                raise MonetaryCostAuthorityError(
                    "receipt correction cannot rewrite campaign membership"
                )
            if receipt.available_at < prior.available_at:
                raise MonetaryCostAuthorityError(
                    "receipt correction cannot become available before predecessor"
                )

        self.store_receipt_candidate(receipt)
        self._bind_source_once(
            namespace=receipt.source_class.value,
            source_authority=receipt.source_authority,
            source_evidence_id=receipt.source_evidence_id,
            record_id=receipt.receipt_id,
        )
        self._write_binding_once(
            self.admissions_dir / f"receipt-{receipt.receipt_id}.json",
            {
                "kind": "RECEIPT",
                "record_id": receipt.receipt_id,
                "resolver_family": resolution.resolver_family.value,
                "resolver_evidence_id": resolution.resolver_evidence_id,
                "resolver_sha256": resolution.resolver_sha256,
            },
            conflict_message="receipt already has a different resolver admission",
        )
        for prior_id in receipt.supersedes_receipt_ids:
            self._write_binding_once(
                self.correction_index_dir / f"{prior_id}.json",
                {"prior_receipt_id": prior_id, "replacement_receipt_id": receipt.receipt_id},
                conflict_message="receipt already has a different append-only correction",
            )
        return receipt.ref

    def publish_currency(self, resolution: ResolvedCampaignCurrency) -> CostSourceRef:
        if type(resolution) is not ResolvedCampaignCurrency:
            raise MonetaryCostAuthorityError(
                "authoritative currency publication requires ResolvedCampaignCurrency capability"
            )
        if resolution.resolver_family is not MonetaryResolverFamily.OWNER_CAMPAIGN_CURRENCY:
            raise MonetaryCostAuthorityError("campaign currency resolver family is invalid")
        _text(resolution.resolver_evidence_id, "resolver_evidence_id")
        _sha256(resolution.resolver_sha256, "resolver_sha256")
        evidence = resolution.evidence
        self.store_currency_candidate(evidence)
        self._bind_source_once(
            namespace="currency",
            source_authority=evidence.source_authority,
            source_evidence_id=evidence.source_evidence_id,
            record_id=evidence.evidence_id,
        )
        self._write_binding_once(
            self.admissions_dir / f"currency-{evidence.evidence_id}.json",
            {
                "kind": "CURRENCY",
                "record_id": evidence.evidence_id,
                "resolver_family": resolution.resolver_family.value,
                "resolver_evidence_id": resolution.resolver_evidence_id,
                "resolver_sha256": resolution.resolver_sha256,
            },
            conflict_message="currency evidence already has a different resolver admission",
        )
        self._write_binding_once(
            self.campaign_currency_index_dir / f"{evidence.campaign_sha256}.json",
            {
                "campaign_sha256": evidence.campaign_sha256,
                "currency": evidence.currency,
                "evidence_id": evidence.evidence_id,
            },
            conflict_message="campaign already has a different canonical currency authority",
        )
        return evidence.ref

    def store_allocation_candidate(self, plan: AllocationPlan) -> CostSourceRef:
        receipt = self._load_receipt(plan.receipt_id)
        self._validate_allocation(plan, receipt)
        self._write_record(
            self.allocations_dir / f"{plan.allocation_id}.json", plan.to_dict()
        )
        return plan.ref

    def publish_allocation(self, plan: AllocationPlan) -> CostSourceRef:
        if type(plan) is not AllocationPlan:
            raise MonetaryCostAuthorityError("allocation has invalid type")
        receipt = self._load_receipt(plan.receipt_id)
        self._require_receipt_admitted(receipt)
        self._validate_allocation(plan, receipt)
        self.store_allocation_candidate(plan)
        self._write_binding_once(
            self.allocation_index_dir / f"{receipt.receipt_id}.json",
            {"receipt_id": receipt.receipt_id, "allocation_id": plan.allocation_id},
            conflict_message="source receipt already has a different allocation plan",
        )
        return plan.ref

    def resolve_campaign_currency(
        self,
        ref: CostSourceRef,
        *,
        campaign_sha256: str,
        as_of: datetime,
    ) -> str:
        if type(ref) is not CostSourceRef or ref.family != CURRENCY_FAMILY:
            raise MonetaryCostAuthorityError("campaign currency reference is not authoritative")
        _sha256(campaign_sha256, "campaign_sha256")
        _utc(as_of, "as_of")
        evidence = self._load_currency(ref.evidence_id)
        self._require_currency_admitted(evidence)
        if evidence.record_sha256 != ref.sha256:
            raise MonetaryCostAuthorityIntegrityError("campaign currency reference digest mismatch")
        if evidence.campaign_sha256 != campaign_sha256:
            raise MonetaryCostAuthorityError("currency evidence belongs to another campaign")
        binding = self._read_record(
            self.campaign_currency_index_dir / f"{campaign_sha256}.json",
            "campaign currency binding",
        )
        if dict(binding) != {
            "campaign_sha256": campaign_sha256,
            "currency": evidence.currency,
            "evidence_id": evidence.evidence_id,
        }:
            raise MonetaryCostAuthorityIntegrityError(
                "campaign currency evidence is not the canonical campaign binding"
            )
        if evidence.available_at > as_of:
            raise MonetaryCostAuthorityError(
                "future-available campaign currency cannot be backdated"
            )
        return evidence.currency

    def build_cost_evidence(
        self,
        *,
        receipt_id: str,
        campaign_sha256: str,
        memberships: Sequence[CanonicalMembershipRef],
        required_interval: tuple[datetime, datetime] | None = None,
    ) -> CostEvidence:
        receipt = self._load_receipt(receipt_id)
        self._require_receipt_admitted(receipt)
        membership_tuple = tuple(memberships)
        _sorted_unique(membership_tuple, "memberships")
        _sha256(campaign_sha256, "campaign_sha256")
        self._validate_required_interval(receipt, required_interval)

        allocation_ref: CostSourceRef | None = None
        money = receipt.money
        observed_at = receipt.observed_at
        available_at = receipt.available_at
        if receipt.shared_source:
            plan = self._allocation_for_receipt(receipt.receipt_id)
            target_matches = [
                item for item in plan.targets
                if item.campaign_sha256 == campaign_sha256
                and item.memberships == membership_tuple
            ]
            if len(target_matches) != 1:
                raise MonetaryCostAuthorityError(
                    "shared receipt has no unique allocation for campaign membership"
                )
            money = target_matches[0].money
            allocation_ref = plan.ref
            observed_at = max(observed_at, plan.observed_at)
            available_at = max(available_at, plan.available_at)
        else:
            if receipt.campaign_sha256 != campaign_sha256:
                raise MonetaryCostAuthorityError("receipt belongs to another campaign")
            if receipt.memberships != membership_tuple:
                raise MonetaryCostAuthorityError("receipt membership mismatch")

        supersedes_cost_ids: list[str] = []
        for prior_id in receipt.supersedes_receipt_ids:
            self._require_correction_binding(prior_id, receipt.receipt_id)
            prior = self.build_cost_evidence(
                receipt_id=prior_id,
                campaign_sha256=campaign_sha256,
                memberships=membership_tuple,
                required_interval=required_interval,
            )
            supersedes_cost_ids.append(prior.cost_evidence_id)

        return CostEvidence(
            cost_class=receipt.cost_class,
            truth=CostTruth.KNOWN_ZERO if money.amount == 0 else CostTruth.KNOWN_AMOUNT,
            basis=CostBasis.OBSERVED_INCURRED,
            treatment=receipt.treatment,
            source=receipt.ref,
            campaign_sha256=campaign_sha256,
            memberships=membership_tuple,
            unit=CostUnit.MONEY,
            currency=money.currency,
            amount=money.amount,
            observed_at=observed_at,
            available_at=available_at,
            incurred_at=receipt.incurred_end,
            shared_source=receipt.shared_source,
            allocation_source=allocation_ref,
            supersedes_cost_evidence_ids=tuple(sorted(supersedes_cost_ids)),
        )

    def qualify_cost(
        self,
        cost: CostEvidence,
        *,
        as_of: datetime,
        required_interval: tuple[datetime, datetime] | None = None,
    ) -> CostEvidence:
        if type(cost) is not CostEvidence:
            raise MonetaryCostAuthorityError("cost evidence has invalid type")
        _utc(as_of, "as_of")
        if cost.source.family != RECEIPT_FAMILY:
            raise MonetaryCostAuthorityError(
                "cost evidence is not backed by the canonical monetary receipt family"
            )
        canonical = self.build_cost_evidence(
            receipt_id=cost.source.evidence_id,
            campaign_sha256=cost.campaign_sha256,
            memberships=cost.memberships,
            required_interval=required_interval,
        )
        if canonical.source.sha256 != cost.source.sha256:
            raise MonetaryCostAuthorityIntegrityError("receipt source digest mismatch")
        if canonical != cost:
            raise MonetaryCostAuthorityError(
                "cost evidence does not equal canonical monetary authority projection"
            )
        if canonical.available_at > as_of:
            raise MonetaryCostAuthorityError(
                "future-available monetary cost cannot be used for this economic version"
            )
        return canonical

    def _validate_allocation(self, plan: AllocationPlan, receipt: MonetaryReceipt) -> None:
        if type(plan) is not AllocationPlan:
            raise MonetaryCostAuthorityError("allocation has invalid type")
        if receipt.record_sha256 != plan.receipt_sha256:
            raise MonetaryCostAuthorityError("allocation references wrong receipt digest")
        if not receipt.shared_source:
            raise MonetaryCostAuthorityError("exclusive receipt cannot have allocation plan")
        if {target.money.currency for target in plan.targets} != {receipt.money.currency}:
            raise MonetaryCostAuthorityError("allocation currency must match source receipt")
        allocated = sum((target.money.amount for target in plan.targets), Decimal("0"))
        if allocated != receipt.money.amount:
            raise MonetaryCostAuthorityError(
                "allocation plan must exactly conserve the source receipt amount"
            )
        if plan.available_at < receipt.available_at:
            raise MonetaryCostAuthorityError(
                "allocation cannot be available before source receipt"
            )

    def _validate_required_interval(
        self,
        receipt: MonetaryReceipt,
        required_interval: tuple[datetime, datetime] | None,
    ) -> None:
        if required_interval is None:
            return
        start, end = required_interval
        _utc(start, "required_interval start")
        _utc(end, "required_interval end")
        if end < start:
            raise MonetaryCostAuthorityError("required_interval end precedes start")
        assert receipt.covered_start is not None and receipt.covered_end is not None
        if receipt.covered_start > start or receipt.covered_end < end:
            raise MonetaryCostAuthorityError(
                "receipt coverage does not contain the required campaign interval"
            )

    def _load_receipt(self, receipt_id: str) -> MonetaryReceipt:
        _sha256(receipt_id, "receipt_id")
        raw = self._read_record(self.receipts_dir / f"{receipt_id}.json", "monetary receipt")
        item = MonetaryReceipt.from_dict(raw)
        if item.receipt_id != receipt_id:
            raise MonetaryCostAuthorityIntegrityError("receipt filename identity mismatch")
        return item

    def _load_currency(self, evidence_id: str) -> CampaignCurrencyEvidence:
        _sha256(evidence_id, "currency evidence_id")
        raw = self._read_record(
            self.currency_dir / f"{evidence_id}.json", "campaign currency evidence"
        )
        item = CampaignCurrencyEvidence.from_dict(raw)
        if item.evidence_id != evidence_id:
            raise MonetaryCostAuthorityIntegrityError("currency filename identity mismatch")
        return item

    def _load_allocation(self, allocation_id: str) -> AllocationPlan:
        _sha256(allocation_id, "allocation_id")
        raw = self._read_record(
            self.allocations_dir / f"{allocation_id}.json", "allocation plan"
        )
        item = AllocationPlan.from_dict(raw)
        if item.allocation_id != allocation_id:
            raise MonetaryCostAuthorityIntegrityError("allocation filename identity mismatch")
        return item

    def _allocation_for_receipt(self, receipt_id: str) -> AllocationPlan:
        raw = self._read_record(
            self.allocation_index_dir / f"{receipt_id}.json", "allocation binding"
        )
        _exact_keys(raw, {"receipt_id", "allocation_id"}, "allocation binding")
        if _string(raw["receipt_id"], "receipt_id") != receipt_id:
            raise MonetaryCostAuthorityIntegrityError("allocation binding receipt mismatch")
        plan = self._load_allocation(_string(raw["allocation_id"], "allocation_id"))
        if plan.receipt_id != receipt_id:
            raise MonetaryCostAuthorityIntegrityError("allocation points to wrong receipt")
        return plan

    def _require_receipt_admitted(self, receipt: MonetaryReceipt) -> None:
        if receipt.quality is not MonetaryEvidenceQuality.INCURRED_ACTUAL:
            raise MonetaryCostAuthorityError(
                "estimate/simulated/unverified evidence cannot mint incurred cost truth"
            )
        raw = self._read_record(
            self.admissions_dir / f"receipt-{receipt.receipt_id}.json",
            "receipt resolver admission",
        )
        family = MonetaryResolverFamily(_string(raw.get("resolver_family"), "resolver_family"))
        _validate_resolver_family(receipt.cost_class, family)
        _exact_keys(
            raw,
            {"kind", "record_id", "resolver_family", "resolver_evidence_id", "resolver_sha256"},
            "receipt resolver admission",
        )
        if raw["kind"] != "RECEIPT" or raw["record_id"] != receipt.receipt_id:
            raise MonetaryCostAuthorityIntegrityError("receipt resolver admission identity mismatch")
        _text(_string(raw["resolver_evidence_id"], "resolver_evidence_id"), "resolver_evidence_id")
        _sha256(_string(raw["resolver_sha256"], "resolver_sha256"), "resolver_sha256")
        self._require_source_bound(
            namespace=receipt.source_class.value,
            source_authority=receipt.source_authority,
            source_evidence_id=receipt.source_evidence_id,
            record_id=receipt.receipt_id,
        )

    def _require_currency_admitted(self, evidence: CampaignCurrencyEvidence) -> None:
        raw = self._read_record(
            self.admissions_dir / f"currency-{evidence.evidence_id}.json",
            "currency resolver admission",
        )
        _exact_keys(
            raw,
            {"kind", "record_id", "resolver_family", "resolver_evidence_id", "resolver_sha256"},
            "currency resolver admission",
        )
        if (
            raw["kind"] != "CURRENCY"
            or raw["record_id"] != evidence.evidence_id
            or raw["resolver_family"] != MonetaryResolverFamily.OWNER_CAMPAIGN_CURRENCY.value
        ):
            raise MonetaryCostAuthorityIntegrityError("currency resolver admission identity mismatch")
        _text(_string(raw["resolver_evidence_id"], "resolver_evidence_id"), "resolver_evidence_id")
        _sha256(_string(raw["resolver_sha256"], "resolver_sha256"), "resolver_sha256")
        self._require_source_bound(
            namespace="currency",
            source_authority=evidence.source_authority,
            source_evidence_id=evidence.source_evidence_id,
            record_id=evidence.evidence_id,
        )

    def _source_index_path(
        self, *, namespace: str, source_authority: str, source_evidence_id: str
    ) -> Path:
        material = "\0".join((namespace, source_authority, source_evidence_id)).encode("utf-8")
        return self.source_index_dir / f"{hashlib.sha256(material).hexdigest()}.json"

    def _bind_source_once(
        self, *, namespace: str, source_authority: str, source_evidence_id: str, record_id: str
    ) -> None:
        self._write_binding_once(
            self._source_index_path(
                namespace=namespace,
                source_authority=source_authority,
                source_evidence_id=source_evidence_id,
            ),
            {
                "namespace": namespace,
                "source_authority": source_authority,
                "source_evidence_id": source_evidence_id,
                "record_id": record_id,
            },
            conflict_message=(
                "same external source identity was presented with conflicting immutable evidence"
            ),
        )

    def _require_source_bound(
        self, *, namespace: str, source_authority: str, source_evidence_id: str, record_id: str
    ) -> None:
        raw = self._read_record(
            self._source_index_path(
                namespace=namespace,
                source_authority=source_authority,
                source_evidence_id=source_evidence_id,
            ),
            "source identity binding",
        )
        expected = {
            "namespace": namespace,
            "source_authority": source_authority,
            "source_evidence_id": source_evidence_id,
            "record_id": record_id,
        }
        if dict(raw) != expected:
            raise MonetaryCostAuthorityIntegrityError(
                "source identity binding does not authorize this record"
            )

    def _require_correction_binding(self, prior_id: str, replacement_id: str) -> None:
        raw = self._read_record(
            self.correction_index_dir / f"{prior_id}.json", "correction binding"
        )
        if dict(raw) != {
            "prior_receipt_id": prior_id,
            "replacement_receipt_id": replacement_id,
        }:
            raise MonetaryCostAuthorityIntegrityError(
                "correction lineage does not authorize this replacement"
            )

    def _write_binding_once(
        self, path: Path, payload: Mapping[str, Any], *, conflict_message: str
    ) -> None:
        if path.exists():
            existing = self._read_record(path, "authority binding")
            if dict(existing) != dict(payload):
                raise MonetaryCostAuthorityError(conflict_message)
            return
        self._write_record(path, payload)

    def _write_record(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = _canonical_json_text(payload)
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing != text:
                raise MonetaryCostAuthorityIntegrityError(
                    "content-addressed authority record was already written differently"
                )
            return
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(path.parent)
        except FileExistsError:
            existing = path.read_text(encoding="utf-8")
            if existing != text:
                raise MonetaryCostAuthorityIntegrityError("concurrent authority record conflict")

    @staticmethod
    def _read_record(path: Path, label: str) -> Mapping[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise MonetaryCostAuthorityIntegrityError(f"missing {label}") from exc
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MonetaryCostAuthorityIntegrityError(f"invalid {label} JSON") from exc
        if not isinstance(raw, dict):
            raise MonetaryCostAuthorityIntegrityError(f"{label} must be a JSON object")
        if _canonical_json_text(raw) != text:
            raise MonetaryCostAuthorityIntegrityError(f"{label} is not canonical JSON")
        return raw


def _validate_source_class(cost_class: CostClass, source_class: MonetarySourceClass) -> None:
    allowed = {
        CostClass.PROVIDER_DATA: MonetarySourceClass.PROVIDER_BILLING,
        CostClass.MODEL_COMPUTE_AI: MonetarySourceClass.COMPUTE_BILLING,
        CostClass.EXECUTION_SLIPPAGE: MonetarySourceClass.EXECUTION_RECEIPT,
        CostClass.EXECUTION_FEES_COMMISSION_TAX: MonetarySourceClass.EXECUTION_RECEIPT,
        CostClass.FIXED_CAMPAIGN: MonetarySourceClass.FIXED_CAMPAIGN_ADMIN,
    }
    if allowed[cost_class] is not source_class:
        raise MonetaryCostAuthorityError(
            f"{source_class.value} cannot authorize {cost_class.value}"
        )


def _validate_resolver_family(cost_class: CostClass, family: MonetaryResolverFamily) -> None:
    allowed = {
        CostClass.PROVIDER_DATA: MonetaryResolverFamily.PROVIDER_ACCOUNT_BILLING,
        CostClass.MODEL_COMPUTE_AI: MonetaryResolverFamily.COMPUTE_BILLING,
        CostClass.EXECUTION_SLIPPAGE: MonetaryResolverFamily.EXECUTION_SETTLEMENT,
        CostClass.EXECUTION_FEES_COMMISSION_TAX: MonetaryResolverFamily.EXECUTION_SETTLEMENT,
        CostClass.FIXED_CAMPAIGN: MonetaryResolverFamily.OWNER_FIXED_EXPENSE,
    }
    if allowed[cost_class] is not family:
        raise MonetaryCostAuthorityError(
            f"{family.value} cannot resolve {cost_class.value}"
        )


def _membership_dict(value: CanonicalMembershipRef) -> dict[str, str]:
    return {"kind": value.kind, "evidence_id": value.evidence_id, "sha256": value.sha256}


def _membership_from_dict(raw: Mapping[str, Any]) -> CanonicalMembershipRef:
    _exact_keys(raw, {"kind", "evidence_id", "sha256"}, "membership")
    return CanonicalMembershipRef(
        kind=_string(raw["kind"], "kind"),
        evidence_id=_string(raw["evidence_id"], "evidence_id"),
        sha256=_string(raw["sha256"], "sha256"),
    )


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise MonetaryCostAuthorityError(f"{label} must be a non-empty canonical string")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MonetaryCostAuthorityError(f"{label} must be lowercase SHA-256 hex")


def _utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MonetaryCostAuthorityError(f"{label} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise MonetaryCostAuthorityError(f"{label} must be expressed in UTC")


def _finite_decimal(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MonetaryCostAuthorityError(f"{label} must be a finite Decimal")


def _sorted_unique(
    values: Sequence[Any], label: str, *, key: Any | None = None
) -> None:
    try:
        if len(set(values)) != len(values):
            raise MonetaryCostAuthorityError(f"duplicate {label}")
    except TypeError as exc:
        raise MonetaryCostAuthorityError(f"{label} must contain immutable values") from exc
    expected = tuple(sorted(values, key=key)) if key is not None else tuple(sorted(values))
    if tuple(values) != expected:
        raise MonetaryCostAuthorityError(f"{label} must be sorted deterministically")


def _exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        raise MonetaryCostAuthorityIntegrityError(
            f"{label} keys mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise MonetaryCostAuthorityIntegrityError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise MonetaryCostAuthorityIntegrityError(f"{label} must be bool")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise MonetaryCostAuthorityIntegrityError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise MonetaryCostAuthorityIntegrityError(f"{label} must be an array")
    return value


def _decimal_text(value: Decimal) -> str:
    _finite_decimal(value, "decimal")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _parse_decimal(value: Any, label: str) -> Decimal:
    text = _string(value, label)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise MonetaryCostAuthorityIntegrityError(
            f"{label} is not Decimal-compatible"
        ) from exc
    _finite_decimal(parsed, label)
    if _decimal_text(parsed) != text:
        raise MonetaryCostAuthorityIntegrityError(f"{label} is not canonical decimal text")
    return parsed


def _datetime_text(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if not text.endswith("Z"):
        raise MonetaryCostAuthorityIntegrityError(f"{label} must use UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise MonetaryCostAuthorityIntegrityError(f"{label} is invalid") from exc
    if _datetime_text(parsed) != text:
        raise MonetaryCostAuthorityIntegrityError(f"{label} is not canonical datetime text")
    return parsed


def _canonical_json_text(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n"


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
