from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .campaign_denomination import (
    CampaignDenominationBinding,
    CampaignDenominationError,
    rehydrate_campaign_denomination_binding,
)
from .campaign_economic_authority import (
    CampaignEconomicAuthorityError,
    CanonicalCampaignProjection,
    CanonicalMembershipRef,
    CanonicalSessionRef,
    FinalizedCampaignAuthority,
)


SCHEMA_VERSION = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _capture_product_denomination_reader():
    # Pin the product implementation once.  Exact-type checks alone do not stop
    # same-process class monkeypatching from replacing a bound method after
    # import and synthesizing positive denomination authority.
    reader = FinalizedCampaignAuthority.denomination_binding

    def resolve(campaign: FinalizedCampaignAuthority):
        return reader(campaign)

    return resolve


_PRODUCT_DENOMINATION_READER = _capture_product_denomination_reader()
del _capture_product_denomination_reader


class CostEvidenceError(ValueError):
    """Raised when campaign economic evidence is malformed or inconsistent."""


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


REQUIRED_COST_CLASSES = tuple(sorted(CostClass, key=lambda value: value.value))


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
class CostSourceRef:
    family: str
    evidence_id: str
    sha256: str

    def __post_init__(self) -> None:
        _text(self.family, "source family")
        _text(self.evidence_id, "source evidence_id")
        _sha256(self.sha256, "source sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "family": self.family,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CostSourceRef":
        _exact_keys(raw, {"family", "evidence_id", "sha256"}, "CostSourceRef")
        return cls(
            family=_string(raw["family"], "family"),
            evidence_id=_string(raw["evidence_id"], "evidence_id"),
            sha256=_string(raw["sha256"], "sha256"),
        )


@dataclass(frozen=True, slots=True)
class CostEvidence:
    cost_class: CostClass
    truth: CostTruth
    basis: CostBasis | None
    treatment: CostTreatment
    source: CostSourceRef
    campaign_sha256: str
    memberships: tuple[CanonicalMembershipRef, ...]
    unit: CostUnit
    currency: str | None
    amount: Decimal | None
    observed_at: datetime
    available_at: datetime
    incurred_at: datetime | None = None
    shared_source: bool = False
    allocation_source: CostSourceRef | None = None
    supersedes_cost_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _sha256(self.campaign_sha256, "campaign_sha256")
        _utc(self.observed_at, "observed_at")
        _utc(self.available_at, "available_at")
        if self.observed_at > self.available_at:
            raise CostEvidenceError("observed_at cannot be later than available_at")
        if self.incurred_at is not None:
            _utc(self.incurred_at, "incurred_at")
            if self.incurred_at > self.available_at:
                raise CostEvidenceError("incurred_at cannot be later than available_at")
        if (
            self.basis is CostBasis.OBSERVED_INCURRED
            and self.truth in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT}
            and self.incurred_at is None
        ):
            raise CostEvidenceError("OBSERVED_INCURRED known cost requires incurred_at")
        _sorted_unique(self.memberships, "memberships")
        _sorted_unique(self.supersedes_cost_evidence_ids, "supersedes ids")
        for evidence_id in self.supersedes_cost_evidence_ids:
            _sha256(evidence_id, "superseded cost evidence id")

        if self.unit is CostUnit.MONEY:
            if self.currency is None or _CURRENCY_RE.fullmatch(self.currency) is None:
                raise CostEvidenceError("money cost requires an uppercase three-letter currency")
        elif self.currency is not None:
            raise CostEvidenceError("non-money cost cannot carry currency")

        if self.amount is not None:
            _finite_decimal(self.amount, "amount")
            if self.amount < 0:
                raise CostEvidenceError("cost amount cannot be negative")

        if self.truth is CostTruth.KNOWN_ZERO:
            if self.amount != Decimal("0") or self.basis is None:
                raise CostEvidenceError("KNOWN_ZERO requires amount=0 and a basis")
        elif self.truth is CostTruth.KNOWN_AMOUNT:
            if self.amount is None or self.basis is None:
                raise CostEvidenceError("KNOWN_AMOUNT requires amount and a basis")
        elif self.truth is CostTruth.UNKNOWN_UNPROVEN:
            if self.amount is not None or self.basis is not None:
                raise CostEvidenceError("UNKNOWN_UNPROVEN cannot carry amount or basis")
            if self.treatment is not CostTreatment.INFORMATIONAL:
                raise CostEvidenceError("UNKNOWN_UNPROVEN must remain INFORMATIONAL")
        elif self.truth is CostTruth.NOT_APPLICABLE:
            if self.amount is not None:
                raise CostEvidenceError("NOT_APPLICABLE cannot carry an amount")
            if self.basis is not CostBasis.AUTHORITATIVE_DECLARATION:
                raise CostEvidenceError("NOT_APPLICABLE requires authoritative declaration basis")
            if self.treatment is not CostTreatment.INFORMATIONAL:
                raise CostEvidenceError("NOT_APPLICABLE must remain INFORMATIONAL")

        if self.shared_source and self.treatment is CostTreatment.SUBTRACT_FROM_GROSS:
            if self.allocation_source is None:
                raise CostEvidenceError("shared subtractive cost requires allocation authority")
        if not self.shared_source and self.allocation_source is not None:
            raise CostEvidenceError("allocation source requires shared_source=true")

    @property
    def cost_evidence_id(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cost_class": self.cost_class.value,
            "truth": self.truth.value,
            "basis": None if self.basis is None else self.basis.value,
            "treatment": self.treatment.value,
            "source": self.source.to_dict(),
            "campaign_sha256": self.campaign_sha256,
            "memberships": [_membership_dict(item) for item in self.memberships],
            "unit": self.unit.value,
            "currency": self.currency,
            "amount": None if self.amount is None else _decimal_text(self.amount),
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
            "incurred_at": None if self.incurred_at is None else _datetime_text(self.incurred_at),
            "shared_source": self.shared_source,
            "allocation_source": None if self.allocation_source is None else self.allocation_source.to_dict(),
            "supersedes_cost_evidence_ids": list(self.supersedes_cost_evidence_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["cost_evidence_id"] = self.cost_evidence_id
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CostEvidence":
        expected = {
            "schema_version", "cost_class", "truth", "basis", "treatment",
            "source", "campaign_sha256", "memberships", "unit", "currency",
            "amount", "observed_at", "available_at", "incurred_at",
            "shared_source", "allocation_source", "supersedes_cost_evidence_ids",
            "cost_evidence_id",
        }
        _exact_keys(raw, expected, "CostEvidence")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise CostEvidenceError("unsupported cost evidence schema_version")
        basis_raw = raw["basis"]
        amount_raw = raw["amount"]
        incurred_raw = raw["incurred_at"]
        allocation_raw = raw["allocation_source"]
        item = cls(
            cost_class=CostClass(_string(raw["cost_class"], "cost_class")),
            truth=CostTruth(_string(raw["truth"], "truth")),
            basis=None if basis_raw is None else CostBasis(_string(basis_raw, "basis")),
            treatment=CostTreatment(_string(raw["treatment"], "treatment")),
            source=CostSourceRef.from_dict(_mapping(raw["source"], "source")),
            campaign_sha256=_string(raw["campaign_sha256"], "campaign_sha256"),
            memberships=tuple(
                _membership_from_dict(_mapping(value, "membership"))
                for value in _list(raw["memberships"], "memberships")
            ),
            unit=CostUnit(_string(raw["unit"], "unit")),
            currency=_optional_string(raw["currency"], "currency"),
            amount=None if amount_raw is None else _parse_decimal(amount_raw, "amount"),
            observed_at=_parse_datetime(raw["observed_at"], "observed_at"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
            incurred_at=None if incurred_raw is None else _parse_datetime(incurred_raw, "incurred_at"),
            shared_source=_bool(raw["shared_source"], "shared_source"),
            allocation_source=None if allocation_raw is None else CostSourceRef.from_dict(_mapping(allocation_raw, "allocation_source")),
            supersedes_cost_evidence_ids=tuple(
                _string(value, "superseded cost evidence id")
                for value in _list(raw["supersedes_cost_evidence_ids"], "supersedes_cost_evidence_ids")
            ),
        )
        if _string(raw["cost_evidence_id"], "cost_evidence_id") != item.cost_evidence_id:
            raise CostEvidenceError("cost evidence digest mismatch")
        return item


@dataclass(frozen=True, slots=True)
class CampaignEconomicEvidenceVersion:
    campaign_authority: CanonicalCampaignProjection
    costs: tuple[CostEvidence, ...]
    as_of: datetime
    previous_version_id: str | None
    previous_version_sha256: str | None
    known_cost_total: Decimal
    net_after_known_costs: Decimal | None
    completeness: EconomicCompleteness
    incomplete_reasons: tuple[str, ...]
    denomination_binding: CampaignDenominationBinding | None = None

    def __post_init__(self) -> None:
        _utc(self.as_of, "as_of")
        _sorted_unique(self.costs, "costs", key=lambda value: value.cost_evidence_id)
        _finite_decimal(self.known_cost_total, "known_cost_total")
        if self.known_cost_total < 0:
            raise CostEvidenceError("known_cost_total cannot be negative")
        if self.net_after_known_costs is not None:
            _finite_decimal(self.net_after_known_costs, "net_after_known_costs")
        _sorted_unique(self.incomplete_reasons, "incomplete_reasons")
        if (
            self.denomination_binding is not None
            and type(self.denomination_binding) is not CampaignDenominationBinding
        ):
            raise CostEvidenceError(
                "denomination_binding must be exact CampaignDenominationBinding or None"
            )
        if (self.previous_version_id is None) != (self.previous_version_sha256 is None):
            raise CostEvidenceError("predecessor id and digest must be present together")
        if self.previous_version_id is not None:
            _sha256(self.previous_version_id, "previous_version_id")
            _sha256(self.previous_version_sha256 or "", "previous_version_sha256")

    @property
    def campaign_sha256(self) -> str:
        return self.campaign_authority.campaign_sha256

    @property
    def gross_run_pnl(self) -> Decimal:
        return self.campaign_authority.gross_run_pnl

    @property
    def version_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.version_id

    def payload(self) -> dict[str, Any]:
        # Preserve byte-identical schema-v3 identity for legacy/non-denominated
        # versions. Positive denomination is an additive extension only when it is
        # product-issued and re-resolvable from canonical completed-run authority.
        raw: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "campaign_authority": _projection_dict(self.campaign_authority),
            "costs": [value.to_dict() for value in self.costs],
            "as_of": _datetime_text(self.as_of),
            "previous_version_id": self.previous_version_id,
            "previous_version_sha256": self.previous_version_sha256,
            "known_cost_total": _decimal_text(self.known_cost_total),
            "net_after_known_costs": None if self.net_after_known_costs is None else _decimal_text(self.net_after_known_costs),
            "completeness": self.completeness.value,
            "incomplete_reasons": list(self.incomplete_reasons),
        }
        if self.denomination_binding is not None:
            raw["denomination_binding"] = dict(
                self.denomination_binding.canonical_payload
            )
        return raw

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["version_id"] = self.version_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CampaignEconomicEvidenceVersion":
        expected = {
            "schema_version", "campaign_authority", "costs", "as_of",
            "previous_version_id", "previous_version_sha256", "known_cost_total",
            "net_after_known_costs", "completeness", "incomplete_reasons",
            "version_id", "record_sha256",
        }
        actual = set(raw)
        optional = {"denomination_binding"}
        if actual not in (expected, expected | optional):
            raise CostEvidenceError(
                "CampaignEconomicEvidenceVersion keys mismatch: "
                f"missing={sorted(expected - actual)}, "
                f"extra={sorted(actual - expected - optional)}"
            )
        if raw["schema_version"] != SCHEMA_VERSION:
            raise CostEvidenceError("unsupported economic evidence schema_version")
        net_raw = raw["net_after_known_costs"]
        item = cls(
            campaign_authority=_projection_from_dict(_mapping(raw["campaign_authority"], "campaign_authority")),
            costs=tuple(
                CostEvidence.from_dict(_mapping(value, "cost"))
                for value in _list(raw["costs"], "costs")
            ),
            as_of=_parse_datetime(raw["as_of"], "as_of"),
            previous_version_id=_optional_string(raw["previous_version_id"], "previous_version_id"),
            previous_version_sha256=_optional_string(raw["previous_version_sha256"], "previous_version_sha256"),
            known_cost_total=_parse_decimal(raw["known_cost_total"], "known_cost_total"),
            net_after_known_costs=None if net_raw is None else _parse_decimal(net_raw, "net_after_known_costs"),
            completeness=EconomicCompleteness(_string(raw["completeness"], "completeness")),
            incomplete_reasons=tuple(
                _string(value, "incomplete reason")
                for value in _list(raw["incomplete_reasons"], "incomplete_reasons")
            ),
            denomination_binding=(
                None
                if "denomination_binding" not in raw
                else rehydrate_campaign_denomination_binding(
                    _mapping(raw["denomination_binding"], "denomination_binding")
                )
            ),
        )
        if _string(raw["version_id"], "version_id") != item.version_id:
            raise CostEvidenceError("economic version id mismatch")
        if _string(raw["record_sha256"], "record_sha256") != item.record_sha256:
            raise CostEvidenceError("economic record digest mismatch")
        return item


def derive_campaign_economics(
    *,
    campaign: FinalizedCampaignAuthority,
    costs: Sequence[CostEvidence],
    as_of: datetime,
    previous: CampaignEconomicEvidenceVersion | None = None,
) -> CampaignEconomicEvidenceVersion:
    """Derive fail-closed campaign economics from canonical campaign truth.

    Campaign denomination is positive only when the finalized product authority
    can re-issue it from transaction-bound completed-run evidence. That closes the
    denomination identity only; caller-authored cost sources, NOT_APPLICABLE
    declarations, configured/public prices and dimensionless compute evidence still
    cannot close professional net-economics truth.
    """

    if type(campaign) is not FinalizedCampaignAuthority:
        raise CostEvidenceError("campaign must be FinalizedCampaignAuthority")
    _utc(as_of, "as_of")
    projection = campaign.projection()
    try:
        denomination = _PRODUCT_DENOMINATION_READER(campaign)
    except (CampaignEconomicAuthorityError, CampaignDenominationError) as exc:
        raise CostEvidenceError(
            "campaign denomination authority failed canonical re-resolution"
        ) from exc
    if denomination is not None and denomination.available_at > as_of:
        raise CostEvidenceError(
            "future-available campaign denomination authority cannot be backdated"
        )
    cost_items = tuple(sorted(costs, key=lambda value: value.cost_evidence_id))
    _sorted_unique(cost_items, "costs", key=lambda value: value.cost_evidence_id)

    allowed_memberships = set(projection.membership_refs)
    seen_sources: set[tuple[str, str, str]] = set()
    for cost in cost_items:
        if cost.campaign_sha256 != projection.campaign_sha256:
            raise CostEvidenceError("cost evidence belongs to a different campaign")
        if (
            denomination is not None
            and cost.unit is CostUnit.MONEY
            and cost.currency != denomination.currency
        ):
            raise CostEvidenceError(
                "money cost currency does not match canonical campaign denomination; "
                "explicit FX authority required"
            )
        if cost.available_at > as_of:
            raise CostEvidenceError("future-available cost evidence cannot be backdated")
        if not set(cost.memberships).issubset(allowed_memberships):
            raise CostEvidenceError("cost membership is outside the finalized campaign")
        source_key = (cost.source.family, cost.source.evidence_id, cost.source.sha256)
        if source_key in seen_sources:
            raise CostEvidenceError("same immutable cost source cannot be counted twice")
        seen_sources.add(source_key)

    if previous is None:
        if any(cost.supersedes_cost_evidence_ids for cost in cost_items):
            raise CostEvidenceError("first economic version cannot supersede prior cost evidence")
    else:
        if previous.campaign_authority != projection:
            raise CostEvidenceError("successor cannot rewrite finalized campaign authority")
        if (
            previous.denomination_binding is not None
            and previous.denomination_binding != denomination
        ):
            raise CostEvidenceError(
                "successor cannot rewrite product-issued campaign denomination"
            )
        if as_of < previous.as_of:
            raise CostEvidenceError("successor cannot move as_of backwards")
        _validate_successor(previous.costs, cost_items)

    superseded = {
        evidence_id
        for cost in cost_items
        for evidence_id in cost.supersedes_cost_evidence_ids
    }
    effective = tuple(
        cost for cost in cost_items if cost.cost_evidence_id not in superseded
    )

    reasons: set[str] = set()
    if denomination is None:
        reasons.add("MISSING_CAMPAIGN_CURRENCY_AUTHORITY")
    by_class: dict[CostClass, list[CostEvidence]] = {
        value: [] for value in REQUIRED_COST_CLASSES
    }
    for cost in effective:
        by_class[cost.cost_class].append(cost)
        reasons.add(f"UNRESOLVED_COST_AUTHORITY:{cost.cost_class.value}")
        if cost.truth is CostTruth.UNKNOWN_UNPROVEN:
            reasons.add(f"UNRESOLVED_COST_CLASS:{cost.cost_class.value}")
        if cost.basis in {CostBasis.CONFIGURED_ESTIMATE, CostBasis.SYNTHETIC_ESTIMATE}:
            reasons.add(f"ESTIMATE_ONLY:{cost.cost_class.value}")
        if cost.unit is not CostUnit.MONEY:
            reasons.add(f"NON_MONEY_UNIT:{cost.cost_class.value}")
        if cost.treatment is CostTreatment.INFORMATIONAL:
            reasons.add(f"INFORMATIONAL_ONLY:{cost.cost_class.value}")

    for cost_class in REQUIRED_COST_CLASSES:
        if not by_class[cost_class]:
            reasons.add(f"MISSING_COST_CLASS:{cost_class.value}")

    return CampaignEconomicEvidenceVersion(
        campaign_authority=projection,
        costs=cost_items,
        as_of=as_of,
        previous_version_id=None if previous is None else previous.version_id,
        previous_version_sha256=None if previous is None else previous.record_sha256,
        known_cost_total=Decimal("0"),
        net_after_known_costs=None,
        completeness=EconomicCompleteness.INCOMPLETE_NET_ECONOMICS,
        incomplete_reasons=tuple(sorted(reasons)),
        denomination_binding=denomination,
    )


def _validate_successor(
    previous: Sequence[CostEvidence], current: Sequence[CostEvidence]
) -> None:
    previous_by_id = {value.cost_evidence_id: value for value in previous}
    current_ids = {value.cost_evidence_id for value in current}
    superseders: dict[str, CostEvidence] = {}
    for value in current:
        retained = previous_by_id.get(value.cost_evidence_id)
        if retained is not None:
            if value != retained:
                raise CostEvidenceError("retained cost evidence changed under the same identity")
            continue
        for superseded_id in value.supersedes_cost_evidence_ids:
            if superseded_id not in previous_by_id:
                raise CostEvidenceError("correction may supersede only predecessor cost evidence")
            if superseded_id in current_ids:
                raise CostEvidenceError("superseded prior cost must not remain active")
            if superseded_id in superseders:
                raise CostEvidenceError("one prior cost cannot have multiple replacements")
            prior = previous_by_id[superseded_id]
            if prior.cost_class is not value.cost_class:
                raise CostEvidenceError("cost correction cannot cross cost class")
            if prior.campaign_sha256 != value.campaign_sha256:
                raise CostEvidenceError("cost correction cannot cross campaign identity")
            if prior.memberships != value.memberships:
                raise CostEvidenceError("cost correction cannot rewrite membership")
            superseders[superseded_id] = value
    for previous_id in previous_by_id:
        if previous_id not in current_ids and previous_id not in superseders:
            raise CostEvidenceError("successor cannot silently drop prior cost evidence")


def _projection_dict(value: CanonicalCampaignProjection) -> dict[str, Any]:
    return {
        "campaign_id": value.campaign_id,
        "campaign_version": value.campaign_version,
        "campaign_sha256": value.campaign_sha256,
        "session_refs": [
            {"evidence_id": item.evidence_id, "evidence_sha256": item.evidence_sha256}
            for item in value.session_refs
        ],
        "membership_refs": [_membership_dict(item) for item in value.membership_refs],
        "gross_run_pnl": _decimal_text(value.gross_run_pnl),
    }


def _projection_from_dict(raw: Mapping[str, Any]) -> CanonicalCampaignProjection:
    _exact_keys(
        raw,
        {
            "campaign_id", "campaign_version", "campaign_sha256", "session_refs",
            "membership_refs", "gross_run_pnl",
        },
        "CanonicalCampaignProjection",
    )
    version = raw["campaign_version"]
    if type(version) is not int or version < 1:
        raise CostEvidenceError("campaign_version must be a positive integer")
    sessions: list[CanonicalSessionRef] = []
    for item in _list(raw["session_refs"], "session_refs"):
        data = _mapping(item, "session_ref")
        _exact_keys(data, {"evidence_id", "evidence_sha256"}, "session_ref")
        sessions.append(
            CanonicalSessionRef(
                evidence_id=_string(data["evidence_id"], "evidence_id"),
                evidence_sha256=_string(data["evidence_sha256"], "evidence_sha256"),
            )
        )
    return CanonicalCampaignProjection(
        campaign_id=_string(raw["campaign_id"], "campaign_id"),
        campaign_version=version,
        campaign_sha256=_string(raw["campaign_sha256"], "campaign_sha256"),
        session_refs=tuple(sessions),
        membership_refs=tuple(
            _membership_from_dict(_mapping(item, "membership_ref"))
            for item in _list(raw["membership_refs"], "membership_refs")
        ),
        gross_run_pnl=_parse_decimal(raw["gross_run_pnl"], "gross_run_pnl"),
    )


def _membership_dict(value: CanonicalMembershipRef) -> dict[str, str]:
    return {"kind": value.kind, "evidence_id": value.evidence_id, "sha256": value.sha256}


def _membership_from_dict(raw: Mapping[str, Any]) -> CanonicalMembershipRef:
    _exact_keys(raw, {"kind", "evidence_id", "sha256"}, "membership_ref")
    return CanonicalMembershipRef(
        kind=_string(raw["kind"], "kind"),
        evidence_id=_string(raw["evidence_id"], "evidence_id"),
        sha256=_string(raw["sha256"], "sha256"),
    )


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CostEvidenceError(f"{label} must be a non-empty canonical string")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CostEvidenceError(f"{label} must be lowercase SHA-256 hex")


def _utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CostEvidenceError(f"{label} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise CostEvidenceError(f"{label} must be expressed in UTC")


def _finite_decimal(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CostEvidenceError(f"{label} must be a finite Decimal")


def _sorted_unique(
    values: Sequence[Any], label: str, *, key: Any | None = None
) -> None:
    try:
        if len(set(values)) != len(values):
            raise CostEvidenceError(f"duplicate {label}")
    except TypeError as exc:
        raise CostEvidenceError(f"{label} must contain immutable values") from exc
    expected = tuple(sorted(values, key=key)) if key is not None else tuple(sorted(values))
    if tuple(values) != expected:
        raise CostEvidenceError(f"{label} must be sorted deterministically")


def _exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        raise CostEvidenceError(
            f"{label} keys mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CostEvidenceError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise CostEvidenceError(f"{label} must be bool")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CostEvidenceError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CostEvidenceError(f"{label} must be an array")
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
        raise CostEvidenceError(f"{label} is not Decimal-compatible") from exc
    _finite_decimal(parsed, label)
    if _decimal_text(parsed) != text:
        raise CostEvidenceError(f"{label} is not canonical decimal text")
    return parsed


def _datetime_text(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if not text.endswith("Z"):
        raise CostEvidenceError(f"{label} must use UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise CostEvidenceError(f"{label} is invalid") from exc
    if _datetime_text(parsed) != text:
        raise CostEvidenceError(f"{label} is not canonical datetime text")
    return parsed


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()