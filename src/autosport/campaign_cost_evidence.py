from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator, Mapping, Protocol, Sequence


SCHEMA_VERSION = 2
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CostEvidenceError(ValueError):
    """Raised when campaign economic evidence is malformed or inconsistent."""


class CostStoreError(RuntimeError):
    """Raised when durable campaign-economic evidence cannot be trusted."""


class CostTruth(StrEnum):
    KNOWN_ZERO = "KNOWN_ZERO"
    KNOWN_AMOUNT = "KNOWN_AMOUNT"
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CostBasis(StrEnum):
    OBSERVED_INCURRED = "OBSERVED_INCURRED"
    EMPIRICAL_DERIVED = "EMPIRICAL_DERIVED"
    AUTHORITATIVE_DECLARATION = "AUTHORITATIVE_DECLARATION"
    CONFIGURED_ESTIMATE = "CONFIGURED_ESTIMATE"
    SYNTHETIC_ESTIMATE = "SYNTHETIC_ESTIMATE"


class CostClass(StrEnum):
    PROVIDER_DATA = "PROVIDER_DATA"
    MODEL_COMPUTE_AI = "MODEL_COMPUTE_AI"
    EXECUTION_SLIPPAGE = "EXECUTION_SLIPPAGE"
    EXECUTION_FEES_COMMISSION_TAX = "EXECUTION_FEES_COMMISSION_TAX"
    FIXED_CAMPAIGN = "FIXED_CAMPAIGN"


REQUIRED_COST_CLASSES = tuple(sorted(CostClass, key=lambda item: item.value))


class CostUnit(StrEnum):
    MONEY = "MONEY"
    COMPUTE_CREDITS = "COMPUTE_CREDITS"
    TOKENS = "TOKENS"
    OTHER = "OTHER"


class CostTreatment(StrEnum):
    SUBTRACT_FROM_GROSS = "SUBTRACT_FROM_GROSS"
    EMBEDDED_IN_GROSS = "EMBEDDED_IN_GROSS"
    INFORMATIONAL = "INFORMATIONAL"


class EconomicCompleteness(StrEnum):
    COMPLETE_NET_ECONOMICS = "COMPLETE_NET_ECONOMICS"
    ESTIMATED_NET_ECONOMICS = "ESTIMATED_NET_ECONOMICS"
    INCOMPLETE_NET_ECONOMICS = "INCOMPLETE_NET_ECONOMICS"


@dataclass(frozen=True, order=True, slots=True)
class AuthorityRef:
    family: str
    evidence_id: str
    sha256: str

    def __post_init__(self) -> None:
        _require_text(self.family, "authority family")
        _require_text(self.evidence_id, "authority evidence_id")
        _require_sha256(self.sha256, "authority sha256")

    def to_dict(self) -> dict[str, str]:
        return {"family": self.family, "evidence_id": self.evidence_id, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuthorityRef":
        _require_exact_keys(payload, {"family", "evidence_id", "sha256"}, "AuthorityRef")
        return cls(
            family=_require_string(payload["family"], "family"),
            evidence_id=_require_string(payload["evidence_id"], "evidence_id"),
            sha256=_require_string(payload["sha256"], "sha256"),
        )


@dataclass(frozen=True, order=True, slots=True)
class MembershipRef:
    kind: str
    evidence_id: str
    sha256: str

    def __post_init__(self) -> None:
        if self.kind not in {"SESSION", "RUN", "EVALUATION"}:
            raise CostEvidenceError("membership kind must be SESSION, RUN, or EVALUATION")
        _require_text(self.evidence_id, "membership evidence_id")
        _require_sha256(self.sha256, "membership sha256")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "evidence_id": self.evidence_id, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MembershipRef":
        _require_exact_keys(payload, {"kind", "evidence_id", "sha256"}, "MembershipRef")
        return cls(
            kind=_require_string(payload["kind"], "kind"),
            evidence_id=_require_string(payload["evidence_id"], "evidence_id"),
            sha256=_require_string(payload["sha256"], "sha256"),
        )


@dataclass(frozen=True, slots=True)
class CostEvidence:
    cost_class: CostClass
    truth: CostTruth
    basis: CostBasis | None
    treatment: CostTreatment
    source: AuthorityRef
    campaign_sha256: str
    memberships: tuple[MembershipRef, ...]
    unit: CostUnit
    currency: str | None
    amount: Decimal | None
    observed_at: datetime
    available_at: datetime
    incurred_at: datetime | None = None
    shared_source: bool = False
    allocation_authority: AuthorityRef | None = None
    supersedes_cost_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_sha256(self.campaign_sha256, "campaign_sha256")
        _require_utc(self.observed_at, "observed_at")
        _require_utc(self.available_at, "available_at")
        if self.observed_at > self.available_at:
            raise CostEvidenceError("observed_at cannot be later than available_at")
        if self.incurred_at is not None:
            _require_utc(self.incurred_at, "incurred_at")
            if self.incurred_at > self.available_at:
                raise CostEvidenceError("incurred_at cannot be later than available_at")
        _require_sorted_unique(self.memberships, "memberships")
        _require_sorted_unique(self.supersedes_cost_evidence_ids, "supersedes_cost_evidence_ids")
        for evidence_id in self.supersedes_cost_evidence_ids:
            _require_sha256(evidence_id, "supersedes cost evidence id")

        if self.unit is CostUnit.MONEY:
            if self.currency is not None and not _CURRENCY_RE.fullmatch(self.currency):
                raise CostEvidenceError("money currency must be an uppercase three-letter code")
        elif self.currency is not None:
            raise CostEvidenceError("non-money cost evidence cannot carry currency")
        if self.amount is not None:
            _require_decimal(self.amount, "amount")
            if self.amount < 0:
                raise CostEvidenceError("cost amount cannot be negative")

        if self.truth is CostTruth.KNOWN_ZERO:
            if self.amount != Decimal("0") or self.basis is None:
                raise CostEvidenceError("KNOWN_ZERO requires amount=0 and a basis")
        elif self.truth is CostTruth.KNOWN_AMOUNT:
            if self.amount is None or self.basis is None:
                raise CostEvidenceError("KNOWN_AMOUNT requires an amount and a basis")
        elif self.truth is CostTruth.UNKNOWN_UNPROVEN:
            if self.amount is not None or self.basis is not None:
                raise CostEvidenceError("UNKNOWN_UNPROVEN cannot carry amount or basis")
            if self.treatment is not CostTreatment.INFORMATIONAL:
                raise CostEvidenceError("UNKNOWN_UNPROVEN must remain INFORMATIONAL")
        elif self.truth is CostTruth.NOT_APPLICABLE:
            if self.amount is not None:
                raise CostEvidenceError("NOT_APPLICABLE cannot carry an amount")
            if self.basis is not CostBasis.AUTHORITATIVE_DECLARATION:
                raise CostEvidenceError("NOT_APPLICABLE requires authoritative declaration evidence")
            if self.treatment is not CostTreatment.INFORMATIONAL:
                raise CostEvidenceError("NOT_APPLICABLE must remain INFORMATIONAL")

        if self.shared_source and self.truth in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT}:
            if self.treatment is CostTreatment.SUBTRACT_FROM_GROSS and self.allocation_authority is None:
                raise CostEvidenceError("shared subtractive cost requires immutable allocation authority")
        if not self.shared_source and self.allocation_authority is not None:
            raise CostEvidenceError("allocation authority is only valid for shared-source cost evidence")

    @property
    def cost_evidence_id(self) -> str:
        return _sha256_json(self.to_payload_dict())

    def to_payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cost_class": self.cost_class.value,
            "truth": self.truth.value,
            "basis": None if self.basis is None else self.basis.value,
            "treatment": self.treatment.value,
            "source": self.source.to_dict(),
            "campaign_sha256": self.campaign_sha256,
            "memberships": [item.to_dict() for item in self.memberships],
            "unit": self.unit.value,
            "currency": self.currency,
            "amount": None if self.amount is None else _decimal_text(self.amount),
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
            "incurred_at": None if self.incurred_at is None else _datetime_text(self.incurred_at),
            "shared_source": self.shared_source,
            "allocation_authority": None if self.allocation_authority is None else self.allocation_authority.to_dict(),
            "supersedes_cost_evidence_ids": list(self.supersedes_cost_evidence_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_payload_dict()
        payload["cost_evidence_id"] = self.cost_evidence_id
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CostEvidence":
        expected = {
            "schema_version", "cost_class", "truth", "basis", "treatment", "source",
            "campaign_sha256", "memberships", "unit", "currency", "amount", "observed_at",
            "available_at", "incurred_at", "shared_source", "allocation_authority",
            "supersedes_cost_evidence_ids", "cost_evidence_id",
        }
        _require_exact_keys(payload, expected, "CostEvidence")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise CostEvidenceError("unsupported cost evidence schema_version")
        allocation = payload["allocation_authority"]
        basis = payload["basis"]
        amount = payload["amount"]
        incurred = payload["incurred_at"]
        item = cls(
            cost_class=CostClass(_require_string(payload["cost_class"], "cost_class")),
            truth=CostTruth(_require_string(payload["truth"], "truth")),
            basis=None if basis is None else CostBasis(_require_string(basis, "basis")),
            treatment=CostTreatment(_require_string(payload["treatment"], "treatment")),
            source=AuthorityRef.from_dict(_require_mapping(payload["source"], "source")),
            campaign_sha256=_require_string(payload["campaign_sha256"], "campaign_sha256"),
            memberships=tuple(MembershipRef.from_dict(_require_mapping(v, "membership")) for v in _require_list(payload["memberships"], "memberships")),
            unit=CostUnit(_require_string(payload["unit"], "unit")),
            currency=_optional_string(payload["currency"], "currency"),
            amount=None if amount is None else _parse_decimal(amount, "amount"),
            observed_at=_parse_datetime(payload["observed_at"], "observed_at"),
            available_at=_parse_datetime(payload["available_at"], "available_at"),
            incurred_at=None if incurred is None else _parse_datetime(incurred, "incurred_at"),
            shared_source=_require_bool(payload["shared_source"], "shared_source"),
            allocation_authority=None if allocation is None else AuthorityRef.from_dict(_require_mapping(allocation, "allocation_authority")),
            supersedes_cost_evidence_ids=tuple(_require_string(v, "supersedes cost evidence id") for v in _require_list(payload["supersedes_cost_evidence_ids"], "supersedes_cost_evidence_ids")),
        )
        stored = _require_string(payload["cost_evidence_id"], "cost_evidence_id")
        if stored != item.cost_evidence_id:
            raise CostEvidenceError("cost evidence digest mismatch")
        return item


@dataclass(frozen=True, slots=True)
class ResolvedCostAuthority:
    """Canonical resolver output. A raw AuthorityRef alone is never sufficient."""

    source: AuthorityRef
    cost_class: CostClass
    truth: CostTruth
    basis: CostBasis | None
    treatment: CostTreatment
    campaign_sha256: str
    memberships: tuple[MembershipRef, ...]
    unit: CostUnit
    currency: str | None
    amount: Decimal | None
    observed_at: datetime
    available_at: datetime
    incurred_at: datetime | None
    shared_source: bool
    allocation_authority: AuthorityRef | None

    def matches(self, evidence: CostEvidence) -> bool:
        return (
            self.source == evidence.source
            and self.cost_class is evidence.cost_class
            and self.truth is evidence.truth
            and self.basis is evidence.basis
            and self.treatment is evidence.treatment
            and self.campaign_sha256 == evidence.campaign_sha256
            and self.memberships == evidence.memberships
            and self.unit is evidence.unit
            and self.currency == evidence.currency
            and self.amount == evidence.amount
            and self.observed_at == evidence.observed_at
            and self.available_at == evidence.available_at
            and self.incurred_at == evidence.incurred_at
            and self.shared_source == evidence.shared_source
            and self.allocation_authority == evidence.allocation_authority
        )


@dataclass(frozen=True, slots=True)
class ResolvedCurrencyAuthority:
    source: AuthorityRef
    campaign_sha256: str
    currency: str

    def __post_init__(self) -> None:
        _require_sha256(self.campaign_sha256, "currency campaign_sha256")
        if not _CURRENCY_RE.fullmatch(self.currency):
            raise CostEvidenceError("resolved campaign currency must be a three-letter code")


class CostAuthorityResolver(Protocol):
    """Bridge to canonical provider/compute/execution/billing authorities.

    Implementations must resolve by immutable id+digest from canonical stores or
    in-process verified capabilities. Returning data reconstructed from the
    caller's CostEvidence is not authoritative.
    """

    def resolve_cost(self, source: AuthorityRef) -> ResolvedCostAuthority | None: ...

    def resolve_currency(self, source: AuthorityRef) -> ResolvedCurrencyAuthority | None: ...


@dataclass(frozen=True, slots=True)
class CampaignEconomicEvidenceVersion:
    campaign_sha256: str
    session_evidence_refs: tuple[AuthorityRef, ...]
    membership_refs: tuple[MembershipRef, ...]
    gross_run_pnl: Decimal
    currency: str | None
    currency_authority: AuthorityRef | None
    costs: tuple[CostEvidence, ...]
    as_of: datetime
    previous_version_id: str | None
    previous_version_sha256: str | None
    known_cost_total: Decimal
    net_after_known_costs: Decimal | None
    completeness: EconomicCompleteness
    incomplete_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.campaign_sha256, "campaign_sha256")
        _require_decimal(self.gross_run_pnl, "gross_run_pnl")
        _require_decimal(self.known_cost_total, "known_cost_total")
        if self.known_cost_total < 0:
            raise CostEvidenceError("known_cost_total cannot be negative")
        if self.net_after_known_costs is not None:
            _require_decimal(self.net_after_known_costs, "net_after_known_costs")
        _require_utc(self.as_of, "as_of")
        if self.currency is not None and not _CURRENCY_RE.fullmatch(self.currency):
            raise CostEvidenceError("campaign currency must be a three-letter code")
        if (self.currency is None) != (self.currency_authority is None):
            raise CostEvidenceError("currency and currency_authority must be present together")
        _require_sorted_unique(self.session_evidence_refs, "session_evidence_refs")
        _require_sorted_unique(self.membership_refs, "membership_refs")
        _require_sorted_unique(self.costs, "costs", key=lambda item: item.cost_evidence_id)
        _require_sorted_unique(self.incomplete_reasons, "incomplete_reasons")
        if (self.previous_version_id is None) != (self.previous_version_sha256 is None):
            raise CostEvidenceError("previous version id and digest must be present together")
        if self.previous_version_id is not None:
            _require_sha256(self.previous_version_id, "previous_version_id")
            _require_sha256(self.previous_version_sha256 or "", "previous_version_sha256")

    @property
    def version_id(self) -> str:
        return _sha256_json(self.to_payload_dict())

    @property
    def record_sha256(self) -> str:
        return self.version_id

    def to_payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "campaign_sha256": self.campaign_sha256,
            "session_evidence_refs": [item.to_dict() for item in self.session_evidence_refs],
            "membership_refs": [item.to_dict() for item in self.membership_refs],
            "gross_run_pnl": _decimal_text(self.gross_run_pnl),
            "currency": self.currency,
            "currency_authority": None if self.currency_authority is None else self.currency_authority.to_dict(),
            "costs": [item.to_dict() for item in self.costs],
            "as_of": _datetime_text(self.as_of),
            "previous_version_id": self.previous_version_id,
            "previous_version_sha256": self.previous_version_sha256,
            "known_cost_total": _decimal_text(self.known_cost_total),
            "net_after_known_costs": None if self.net_after_known_costs is None else _decimal_text(self.net_after_known_costs),
            "completeness": self.completeness.value,
            "incomplete_reasons": list(self.incomplete_reasons),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_payload_dict()
        payload["version_id"] = self.version_id
        payload["record_sha256"] = self.record_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CampaignEconomicEvidenceVersion":
        expected = {
            "schema_version", "campaign_sha256", "session_evidence_refs", "membership_refs",
            "gross_run_pnl", "currency", "currency_authority", "costs", "as_of",
            "previous_version_id", "previous_version_sha256", "known_cost_total",
            "net_after_known_costs", "completeness", "incomplete_reasons", "version_id",
            "record_sha256",
        }
        _require_exact_keys(payload, expected, "CampaignEconomicEvidenceVersion")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise CostEvidenceError("unsupported campaign economic evidence schema_version")
        currency_authority = payload["currency_authority"]
        net = payload["net_after_known_costs"]
        item = cls(
            campaign_sha256=_require_string(payload["campaign_sha256"], "campaign_sha256"),
            session_evidence_refs=tuple(AuthorityRef.from_dict(_require_mapping(v, "session evidence ref")) for v in _require_list(payload["session_evidence_refs"], "session_evidence_refs")),
            membership_refs=tuple(MembershipRef.from_dict(_require_mapping(v, "membership ref")) for v in _require_list(payload["membership_refs"], "membership_refs")),
            gross_run_pnl=_parse_decimal(payload["gross_run_pnl"], "gross_run_pnl"),
            currency=_optional_string(payload["currency"], "currency"),
            currency_authority=None if currency_authority is None else AuthorityRef.from_dict(_require_mapping(currency_authority, "currency_authority")),
            costs=tuple(CostEvidence.from_dict(_require_mapping(v, "cost")) for v in _require_list(payload["costs"], "costs")),
            as_of=_parse_datetime(payload["as_of"], "as_of"),
            previous_version_id=_optional_string(payload["previous_version_id"], "previous_version_id"),
            previous_version_sha256=_optional_string(payload["previous_version_sha256"], "previous_version_sha256"),
            known_cost_total=_parse_decimal(payload["known_cost_total"], "known_cost_total"),
            net_after_known_costs=None if net is None else _parse_decimal(net, "net_after_known_costs"),
            completeness=EconomicCompleteness(_require_string(payload["completeness"], "completeness")),
            incomplete_reasons=tuple(_require_string(v, "incomplete reason") for v in _require_list(payload["incomplete_reasons"], "incomplete_reasons")),
        )
        if _require_string(payload["version_id"], "version_id") != item.version_id:
            raise CostEvidenceError("economic version id mismatch")
        if _require_string(payload["record_sha256"], "record_sha256") != item.record_sha256:
            raise CostEvidenceError("economic record digest mismatch")
        return item


def derive_campaign_economics(
    *,
    campaign_sha256: str,
    session_evidence_refs: Sequence[AuthorityRef],
    membership_refs: Sequence[MembershipRef],
    gross_run_pnl: Decimal,
    currency: str | None,
    currency_authority: AuthorityRef | None,
    costs: Sequence[CostEvidence],
    as_of: datetime,
    resolver: CostAuthorityResolver,
    previous: CampaignEconomicEvidenceVersion | None = None,
) -> CampaignEconomicEvidenceVersion:
    _require_sha256(campaign_sha256, "campaign_sha256")
    _require_decimal(gross_run_pnl, "gross_run_pnl")
    _require_utc(as_of, "as_of")
    session_refs = tuple(sorted(session_evidence_refs))
    memberships = tuple(sorted(membership_refs))
    cost_items = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))
    _require_sorted_unique(session_refs, "session_evidence_refs")
    _require_sorted_unique(memberships, "membership_refs")
    _require_sorted_unique(cost_items, "costs", key=lambda item: item.cost_evidence_id)

    reasons: set[str] = set()
    estimated = False
    if currency is None or currency_authority is None:
        reasons.add("MISSING_CAMPAIGN_CURRENCY_AUTHORITY")
    else:
        if not _CURRENCY_RE.fullmatch(currency):
            raise CostEvidenceError("campaign currency must be a three-letter code")
        resolved_currency = resolver.resolve_currency(currency_authority)
        if (
            resolved_currency is None
            or resolved_currency.source != currency_authority
            or resolved_currency.campaign_sha256 != campaign_sha256
            or resolved_currency.currency != currency
        ):
            reasons.add("UNRESOLVED_CAMPAIGN_CURRENCY_AUTHORITY")

    allowed_memberships = set(memberships)
    source_keys: set[tuple[str, str, str]] = set()
    resolved_by_id: dict[str, ResolvedCostAuthority] = {}
    for cost in cost_items:
        if cost.campaign_sha256 != campaign_sha256:
            raise CostEvidenceError("cost evidence belongs to a different campaign")
        if cost.available_at > as_of:
            raise CostEvidenceError("future-available cost evidence cannot be backdated into this version")
        if not set(cost.memberships).issubset(allowed_memberships):
            raise CostEvidenceError("cost evidence contains membership outside the finalized campaign")
        source_key = (cost.source.family, cost.source.evidence_id, cost.source.sha256)
        if source_key in source_keys:
            raise CostEvidenceError("same immutable source cost evidence cannot appear twice")
        source_keys.add(source_key)
        resolved = resolver.resolve_cost(cost.source)
        if resolved is None or not resolved.matches(cost):
            reasons.add(f"UNRESOLVED_SOURCE_AUTHORITY:{cost.cost_class.value}:{cost.cost_evidence_id}")
        else:
            resolved_by_id[cost.cost_evidence_id] = resolved

    if previous is not None:
        if previous.campaign_sha256 != campaign_sha256:
            raise CostEvidenceError("successor version cannot cross campaign identity")
        if previous.session_evidence_refs != session_refs or previous.membership_refs != memberships:
            raise CostEvidenceError("successor version cannot rewrite finalized membership")
        if previous.gross_run_pnl != gross_run_pnl:
            raise CostEvidenceError("cost correction cannot rewrite authoritative gross run P&L")
        if previous.currency != currency or previous.currency_authority != currency_authority:
            raise CostEvidenceError("cost correction cannot silently rewrite campaign currency authority")
        if as_of < previous.as_of:
            raise CostEvidenceError("successor economic evidence cannot move as_of backwards")
        _validate_successor_costs(previous.costs, cost_items)

    superseded_ids = {superseded for item in cost_items for superseded in item.supersedes_cost_evidence_ids}
    effective = tuple(item for item in cost_items if item.cost_evidence_id not in superseded_ids)

    known_total = Decimal("0")
    by_class: dict[CostClass, list[CostEvidence]] = {item: [] for item in REQUIRED_COST_CLASSES}
    for cost in effective:
        by_class[cost.cost_class].append(cost)
        resolved = resolved_by_id.get(cost.cost_evidence_id)
        if resolved is None:
            continue
        if cost.truth in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT}:
            if cost.basis in {CostBasis.CONFIGURED_ESTIMATE, CostBasis.SYNTHETIC_ESTIMATE}:
                estimated = True
            if cost.treatment is CostTreatment.INFORMATIONAL:
                reasons.add(f"INFORMATIONAL_ONLY:{cost.cost_class.value}")
                continue
            if cost.unit is not CostUnit.MONEY:
                reasons.add(f"NON_MONEY_UNIT:{cost.cost_class.value}")
                continue
            if currency is None or cost.currency is None:
                reasons.add(f"UNRESOLVED_CURRENCY:{cost.cost_class.value}")
                continue
            if cost.currency != currency:
                reasons.add(f"CROSS_CURRENCY:{cost.cost_class.value}:{cost.currency}")
                continue
            if cost.treatment is CostTreatment.SUBTRACT_FROM_GROSS:
                known_total += cost.amount or Decimal("0")

    for cost_class in REQUIRED_COST_CLASSES:
        evidence = by_class[cost_class]
        if not evidence:
            reasons.add(f"MISSING_COST_CLASS:{cost_class.value}")
            continue
        if any(item.cost_evidence_id not in resolved_by_id for item in evidence):
            reasons.add(f"UNVERIFIED_COST_CLASS:{cost_class.value}")
            continue
        if any(item.truth is CostTruth.UNKNOWN_UNPROVEN for item in evidence):
            reasons.add(f"UNRESOLVED_COST_CLASS:{cost_class.value}")
            continue
        applicable = [item for item in evidence if item.truth is not CostTruth.NOT_APPLICABLE]
        if not applicable:
            continue
        if not any(item.truth in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT} for item in applicable):
            reasons.add(f"NO_KNOWN_COST:{cost_class.value}")

    net_after_known = None if currency is None else gross_run_pnl - known_total
    if reasons:
        completeness = EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    elif estimated:
        completeness = EconomicCompleteness.ESTIMATED_NET_ECONOMICS
    else:
        completeness = EconomicCompleteness.COMPLETE_NET_ECONOMICS

    return CampaignEconomicEvidenceVersion(
        campaign_sha256=campaign_sha256,
        session_evidence_refs=session_refs,
        membership_refs=memberships,
        gross_run_pnl=gross_run_pnl,
        currency=currency,
        currency_authority=currency_authority,
        costs=cost_items,
        as_of=as_of,
        previous_version_id=None if previous is None else previous.version_id,
        previous_version_sha256=None if previous is None else previous.record_sha256,
        known_cost_total=known_total,
        net_after_known_costs=net_after_known,
        completeness=completeness,
        incomplete_reasons=tuple(sorted(reasons)),
    )


def _validate_successor_costs(previous: Sequence[CostEvidence], current: Sequence[CostEvidence]) -> None:
    previous_by_id = {item.cost_evidence_id: item for item in previous}
    current_ids = {item.cost_evidence_id for item in current}
    superseders: dict[str, CostEvidence] = {}
    for item in current:
        for superseded_id in item.supersedes_cost_evidence_ids:
            if superseded_id not in previous_by_id:
                raise CostEvidenceError("cost correction may supersede only evidence from the immediate predecessor")
            if superseded_id in current_ids:
                raise CostEvidenceError("superseded prior cost must not remain active in the successor")
            if superseded_id in superseders:
                raise CostEvidenceError("one prior cost cannot be superseded by multiple successor records")
            prior = previous_by_id[superseded_id]
            if prior.cost_class is not item.cost_class:
                raise CostEvidenceError("cost correction cannot cross cost class")
            if prior.campaign_sha256 != item.campaign_sha256 or prior.memberships != item.memberships:
                raise CostEvidenceError("cost correction cannot rewrite campaign membership")
            superseders[superseded_id] = item
    for prior_id in previous_by_id:
        if prior_id not in current_ids and prior_id not in superseders:
            raise CostEvidenceError("successor cannot silently drop prior cost evidence")


class CampaignEconomicEvidenceStore:
    """Append-only sidecar that re-derives every claimed aggregate before trust."""

    def __init__(self, root: str | os.PathLike[str], *, resolver: CostAuthorityResolver) -> None:
        self.root = Path(root)
        self.resolver = resolver

    def append(self, version: CampaignEconomicEvidenceVersion) -> str:
        campaign_dir = self._campaign_dir(version.campaign_sha256)
        versions_dir = campaign_dir / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(campaign_dir / ".publish.lock"):
            current = self.latest(version.campaign_sha256)
            if current is None:
                if version.previous_version_id is not None:
                    raise CostStoreError("first stored version cannot name a predecessor")
            else:
                if version.version_id == current.version_id:
                    return version.version_id
                if version.previous_version_id != current.version_id or version.previous_version_sha256 != current.record_sha256:
                    raise CostStoreError("economic evidence successor does not extend the current head")
            self._validate_derived(version, current)
            path = versions_dir / f"{version.version_id}.json"
            encoded = _canonical_json_bytes(version.to_dict())
            if path.exists():
                if path.read_bytes() != encoded:
                    raise CostStoreError("existing immutable economic version conflicts with retry")
            else:
                _create_immutable_file(path, encoded)
            _atomic_replace_json(
                campaign_dir / "head.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "campaign_sha256": version.campaign_sha256,
                    "version_id": version.version_id,
                    "record_sha256": version.record_sha256,
                },
            )
            return version.version_id

    def latest(self, campaign_sha256: str) -> CampaignEconomicEvidenceVersion | None:
        _require_sha256(campaign_sha256, "campaign_sha256")
        head_path = self._campaign_dir(campaign_sha256) / "head.json"
        if not head_path.exists():
            return None
        head = _strict_json_bytes(head_path.read_bytes(), "economic head")
        _require_exact_keys(head, {"schema_version", "campaign_sha256", "version_id", "record_sha256"}, "economic head")
        if head["schema_version"] != SCHEMA_VERSION or head["campaign_sha256"] != campaign_sha256:
            raise CostStoreError("economic head identity/schema mismatch")
        version_id = _require_string(head["version_id"], "head version_id")
        if _require_string(head["record_sha256"], "head record_sha256") != version_id:
            raise CostStoreError("economic head digest mismatch")
        latest = self._load_raw(campaign_sha256, version_id)
        chain = self._validate_chain_from_head(latest)
        all_ids = self._version_file_ids(campaign_sha256)
        if all_ids != {item.version_id for item in chain}:
            raise CostStoreError("economic store contains orphaned or rolled-back version history")
        return latest

    def load(self, campaign_sha256: str, version_id: str) -> CampaignEconomicEvidenceVersion:
        target = self._load_raw(campaign_sha256, version_id)
        chain = self._validate_chain_from_head(target)
        return chain[-1]

    def verify_chain(self, campaign_sha256: str) -> tuple[CampaignEconomicEvidenceVersion, ...]:
        latest = self.latest(campaign_sha256)
        if latest is None:
            return ()
        return self._validate_chain_from_head(latest)

    def _validate_chain_from_head(self, head: CampaignEconomicEvidenceVersion) -> tuple[CampaignEconomicEvidenceVersion, ...]:
        reverse: list[CampaignEconomicEvidenceVersion] = []
        seen: set[str] = set()
        current = head
        while True:
            if current.version_id in seen:
                raise CostStoreError("economic evidence chain contains a cycle")
            seen.add(current.version_id)
            previous = None
            if current.previous_version_id is not None:
                previous = self._load_raw(current.campaign_sha256, current.previous_version_id)
                if current.previous_version_sha256 != previous.record_sha256:
                    raise CostStoreError("economic evidence predecessor digest mismatch")
            self._validate_derived(current, previous)
            reverse.append(current)
            if previous is None:
                break
            current = previous
        reverse.reverse()
        return tuple(reverse)

    def _validate_derived(self, version: CampaignEconomicEvidenceVersion, previous: CampaignEconomicEvidenceVersion | None) -> None:
        expected = derive_campaign_economics(
            campaign_sha256=version.campaign_sha256,
            session_evidence_refs=version.session_evidence_refs,
            membership_refs=version.membership_refs,
            gross_run_pnl=version.gross_run_pnl,
            currency=version.currency,
            currency_authority=version.currency_authority,
            costs=version.costs,
            as_of=version.as_of,
            resolver=self.resolver,
            previous=previous,
        )
        if expected != version:
            raise CostStoreError("campaign economic derived fields do not match canonical re-derivation")

    def _load_raw(self, campaign_sha256: str, version_id: str) -> CampaignEconomicEvidenceVersion:
        _require_sha256(campaign_sha256, "campaign_sha256")
        _require_sha256(version_id, "version_id")
        path = self._campaign_dir(campaign_sha256) / "versions" / f"{version_id}.json"
        try:
            payload = _strict_json_bytes(path.read_bytes(), "economic version")
        except FileNotFoundError as exc:
            raise CostStoreError("economic evidence version is missing") from exc
        try:
            version = CampaignEconomicEvidenceVersion.from_dict(payload)
        except (CostEvidenceError, ValueError, TypeError) as exc:
            raise CostStoreError("economic evidence version failed integrity validation") from exc
        if version.campaign_sha256 != campaign_sha256 or version.version_id != version_id:
            raise CostStoreError("economic evidence path identity mismatch")
        return version

    def _version_file_ids(self, campaign_sha256: str) -> set[str]:
        directory = self._campaign_dir(campaign_sha256) / "versions"
        if not directory.exists():
            return set()
        ids: set[str] = set()
        for path in directory.glob("*.json"):
            version_id = path.stem
            _require_sha256(version_id, "version filename")
            ids.add(version_id)
        return ids

    def _campaign_dir(self, campaign_sha256: str) -> Path:
        return self.root / "campaign_economics" / campaign_sha256


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CostEvidenceError(f"{label} must be a non-empty trimmed string")


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise CostEvidenceError(f"{label} must be a lowercase sha256 hex digest")


def _require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CostEvidenceError(f"{label} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise CostEvidenceError(f"{label} must be expressed in UTC")


def _require_decimal(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CostEvidenceError(f"{label} must be a finite Decimal")


def _require_sorted_unique(values: Sequence[Any], label: str, *, key: Any | None = None) -> None:
    try:
        if len(set(values)) != len(values):
            raise CostEvidenceError(f"duplicate {label}")
    except TypeError as exc:
        raise CostEvidenceError(f"{label} must contain hashable immutable values") from exc
    expected = tuple(sorted(values, key=key)) if key is not None else tuple(sorted(values))
    if tuple(values) != expected:
        raise CostEvidenceError(f"{label} must be sorted deterministically")


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload.keys())
    if actual != expected:
        raise CostEvidenceError(f"{label} keys mismatch: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CostEvidenceError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _require_string(value, label)


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise CostEvidenceError(f"{label} must be a bool")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CostEvidenceError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CostEvidenceError(f"{label} must be an array")
    return value


def _decimal_text(value: Decimal) -> str:
    _require_decimal(value, "decimal")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _parse_decimal(value: Any, label: str) -> Decimal:
    text = _require_string(value, label)
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise CostEvidenceError(f"{label} is not a Decimal") from exc
    _require_decimal(result, label)
    if _decimal_text(result) != text:
        raise CostEvidenceError(f"{label} is not in canonical decimal form")
    return result


def _datetime_text(value: datetime) -> str:
    _require_utc(value, "datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _require_string(value, label)
    if not text.endswith("Z"):
        raise CostEvidenceError(f"{label} must use canonical UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise CostEvidenceError(f"{label} is not a valid datetime") from exc
    if _datetime_text(parsed) != text:
        raise CostEvidenceError(f"{label} is not in canonical datetime form")
    return parsed


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _strict_json_bytes(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CostStoreError(f"{label} is not UTF-8") from exc

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CostStoreError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(CostStoreError(f"{label} contains non-standard numeric constant {value}")),
        )
    except json.JSONDecodeError as exc:
        raise CostStoreError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CostStoreError(f"{label} must contain a JSON object")
    return payload


def _create_immutable_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _atomic_replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0))
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CostStoreError("campaign economic evidence publication lock is busy") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise CostStoreError("campaign economic evidence publication lock is busy") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
