from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CostEvidenceError(ValueError):
    """Raised when campaign economic evidence is malformed or inconsistent."""


class CostStoreError(RuntimeError):
    """Raised when durable campaign-economic evidence cannot be trusted."""


class CostTruth(str, Enum):
    KNOWN_ZERO = "KNOWN_ZERO"
    KNOWN_AMOUNT = "KNOWN_AMOUNT"
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CostBasis(str, Enum):
    OBSERVED_INCURRED = "OBSERVED_INCURRED"
    EMPIRICAL_DERIVED = "EMPIRICAL_DERIVED"
    AUTHORITATIVE_DECLARATION = "AUTHORITATIVE_DECLARATION"
    CONFIGURED_ESTIMATE = "CONFIGURED_ESTIMATE"
    SYNTHETIC_ESTIMATE = "SYNTHETIC_ESTIMATE"


class CostClass(str, Enum):
    PROVIDER_DATA = "PROVIDER_DATA"
    MODEL_COMPUTE_AI = "MODEL_COMPUTE_AI"
    EXECUTION_SLIPPAGE = "EXECUTION_SLIPPAGE"
    EXECUTION_FEES_COMMISSION_TAX = "EXECUTION_FEES_COMMISSION_TAX"
    FIXED_CAMPAIGN = "FIXED_CAMPAIGN"


class CostUnit(str, Enum):
    MONEY = "MONEY"
    COMPUTE_CREDITS = "COMPUTE_CREDITS"
    TOKENS = "TOKENS"
    OTHER = "OTHER"


class CostTreatment(str, Enum):
    """How a cost participates in the authoritative campaign P&L.

    SUBTRACT_FROM_GROSS is for costs not already represented by the existing
    authority-validated run P&L. EMBEDDED_IN_GROSS records a known economic
    cost that is already reflected in the accepted-price/run-P&L authority and
    therefore must not be subtracted a second time. INFORMATIONAL is retained
    for audit evidence but is not sufficient by itself to close a required
    economic cost class.
    """

    SUBTRACT_FROM_GROSS = "SUBTRACT_FROM_GROSS"
    EMBEDDED_IN_GROSS = "EMBEDDED_IN_GROSS"
    INFORMATIONAL = "INFORMATIONAL"


class EconomicCompleteness(str, Enum):
    COMPLETE_NET_ECONOMICS = "COMPLETE_NET_ECONOMICS"
    ESTIMATED_NET_ECONOMICS = "ESTIMATED_NET_ECONOMICS"
    INCOMPLETE_NET_ECONOMICS = "INCOMPLETE_NET_ECONOMICS"


@dataclass(frozen=True, order=True)
class AuthorityRef:
    family: str
    evidence_id: str
    sha256: str

    def __post_init__(self) -> None:
        _require_text(self.family, "authority family")
        _require_text(self.evidence_id, "authority evidence_id")
        _require_sha256(self.sha256, "authority sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "family": self.family,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuthorityRef":
        _require_exact_keys(payload, {"family", "evidence_id", "sha256"}, "AuthorityRef")
        return cls(
            family=_require_string(payload["family"], "family"),
            evidence_id=_require_string(payload["evidence_id"], "evidence_id"),
            sha256=_require_string(payload["sha256"], "sha256"),
        )


@dataclass(frozen=True, order=True)
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
        return {
            "kind": self.kind,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MembershipRef":
        _require_exact_keys(payload, {"kind", "evidence_id", "sha256"}, "MembershipRef")
        return cls(
            kind=_require_string(payload["kind"], "kind"),
            evidence_id=_require_string(payload["evidence_id"], "evidence_id"),
            sha256=_require_string(payload["sha256"], "sha256"),
        )


@dataclass(frozen=True)
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

    def __post_init__(self) -> None:
        _require_sha256(self.campaign_sha256, "campaign_sha256")
        _require_utc(self.observed_at, "observed_at")
        _require_utc(self.available_at, "available_at")
        if self.incurred_at is not None:
            _require_utc(self.incurred_at, "incurred_at")
            if self.incurred_at > self.available_at:
                raise CostEvidenceError("incurred_at cannot be later than available_at")
        if self.observed_at > self.available_at:
            raise CostEvidenceError("observed_at cannot be later than available_at")

        if len(set(self.memberships)) != len(self.memberships):
            raise CostEvidenceError("duplicate membership reference")
        if tuple(sorted(self.memberships)) != self.memberships:
            raise CostEvidenceError("memberships must be sorted deterministically")

        if self.unit is CostUnit.MONEY:
            if self.currency is not None and not _CURRENCY_RE.fullmatch(self.currency):
                raise CostEvidenceError("money currency must be an uppercase ISO-like three-letter code")
        elif self.currency is not None:
            raise CostEvidenceError("non-money cost evidence cannot carry currency")

        if self.amount is not None:
            _require_decimal(self.amount, "amount")
            if self.amount < 0:
                raise CostEvidenceError("cost amount cannot be negative")

        if self.truth is CostTruth.KNOWN_ZERO:
            if self.amount != Decimal("0"):
                raise CostEvidenceError("KNOWN_ZERO requires amount=0")
            if self.basis is None:
                raise CostEvidenceError("known cost truth requires a basis")
        elif self.truth is CostTruth.KNOWN_AMOUNT:
            if self.amount is None:
                raise CostEvidenceError("KNOWN_AMOUNT requires an amount")
            if self.basis is None:
                raise CostEvidenceError("known cost truth requires a basis")
        elif self.truth is CostTruth.UNKNOWN_UNPROVEN:
            if self.amount is not None:
                raise CostEvidenceError("UNKNOWN_UNPROVEN cannot carry an amount")
            if self.basis is not None:
                raise CostEvidenceError("UNKNOWN_UNPROVEN cannot imply a basis")
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
                raise CostEvidenceError(
                    "shared subtractive cost requires immutable allocation authority; heuristic proration is forbidden"
                )
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
            "allocation_authority": (
                None if self.allocation_authority is None else self.allocation_authority.to_dict()
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_payload_dict()
        payload["cost_evidence_id"] = self.cost_evidence_id
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CostEvidence":
        expected = {
            "schema_version",
            "cost_class",
            "truth",
            "basis",
            "treatment",
            "source",
            "campaign_sha256",
            "memberships",
            "unit",
            "currency",
            "amount",
            "observed_at",
            "available_at",
            "incurred_at",
            "shared_source",
            "allocation_authority",
            "cost_evidence_id",
        }
        _require_exact_keys(payload, expected, "CostEvidence")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise CostEvidenceError("unsupported cost evidence schema_version")
        source = _require_mapping(payload["source"], "source")
        memberships_payload = _require_list(payload["memberships"], "memberships")
        allocation_payload = payload["allocation_authority"]
        basis_raw = payload["basis"]
        amount_raw = payload["amount"]
        incurred_raw = payload["incurred_at"]
        item = cls(
            cost_class=CostClass(_require_string(payload["cost_class"], "cost_class")),
            truth=CostTruth(_require_string(payload["truth"], "truth")),
            basis=None if basis_raw is None else CostBasis(_require_string(basis_raw, "basis")),
            treatment=CostTreatment(_require_string(payload["treatment"], "treatment")),
            source=AuthorityRef.from_dict(source),
            campaign_sha256=_require_string(payload["campaign_sha256"], "campaign_sha256"),
            memberships=tuple(MembershipRef.from_dict(_require_mapping(v, "membership")) for v in memberships_payload),
            unit=CostUnit(_require_string(payload["unit"], "unit")),
            currency=_optional_string(payload["currency"], "currency"),
            amount=None if amount_raw is None else _parse_decimal(amount_raw, "amount"),
            observed_at=_parse_datetime(payload["observed_at"], "observed_at"),
            available_at=_parse_datetime(payload["available_at"], "available_at"),
            incurred_at=None if incurred_raw is None else _parse_datetime(incurred_raw, "incurred_at"),
            shared_source=_require_bool(payload["shared_source"], "shared_source"),
            allocation_authority=(
                None
                if allocation_payload is None
                else AuthorityRef.from_dict(_require_mapping(allocation_payload, "allocation_authority"))
            ),
        )
        stored_id = _require_string(payload["cost_evidence_id"], "cost_evidence_id")
        _require_sha256(stored_id, "cost_evidence_id")
        if stored_id != item.cost_evidence_id:
            raise CostEvidenceError("cost evidence digest mismatch")
        return item


@dataclass(frozen=True)
class CampaignEconomicEvidenceVersion:
    campaign_sha256: str
    session_evidence_refs: tuple[AuthorityRef, ...]
    membership_refs: tuple[MembershipRef, ...]
    gross_run_pnl: Decimal
    currency: str | None
    currency_authority: AuthorityRef | None
    required_cost_classes: tuple[CostClass, ...]
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
            raise CostEvidenceError("campaign currency must be an uppercase ISO-like three-letter code")
        if (self.currency is None) != (self.currency_authority is None):
            raise CostEvidenceError("campaign currency and currency_authority must be present together")

        _require_sorted_unique(self.session_evidence_refs, "session_evidence_refs")
        _require_sorted_unique(self.membership_refs, "membership_refs")
        _require_sorted_unique(self.required_cost_classes, "required_cost_classes", key=lambda v: v.value)
        _require_sorted_unique(self.costs, "costs", key=lambda v: v.cost_evidence_id)
        if tuple(sorted(self.incomplete_reasons)) != self.incomplete_reasons:
            raise CostEvidenceError("incomplete_reasons must be sorted deterministically")
        if len(set(self.incomplete_reasons)) != len(self.incomplete_reasons):
            raise CostEvidenceError("duplicate incomplete reason")

        if (self.previous_version_id is None) != (self.previous_version_sha256 is None):
            raise CostEvidenceError("previous version id and digest must be present together")
        if self.previous_version_id is not None:
            _require_sha256(self.previous_version_id, "previous_version_id")
            _require_sha256(self.previous_version_sha256 or "", "previous_version_sha256")

        cost_ids = [item.cost_evidence_id for item in self.costs]
        if len(cost_ids) != len(set(cost_ids)):
            raise CostEvidenceError("duplicate cost evidence id")
        source_keys = [
            (item.source.family, item.source.evidence_id, item.source.sha256)
            for item in self.costs
        ]
        if len(source_keys) != len(set(source_keys)):
            raise CostEvidenceError("the same immutable source cost evidence cannot be counted twice")

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
            "currency_authority": (
                None if self.currency_authority is None else self.currency_authority.to_dict()
            ),
            "required_cost_classes": [item.value for item in self.required_cost_classes],
            "costs": [item.to_dict() for item in self.costs],
            "as_of": _datetime_text(self.as_of),
            "previous_version_id": self.previous_version_id,
            "previous_version_sha256": self.previous_version_sha256,
            "known_cost_total": _decimal_text(self.known_cost_total),
            "net_after_known_costs": (
                None if self.net_after_known_costs is None else _decimal_text(self.net_after_known_costs)
            ),
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
            "schema_version",
            "campaign_sha256",
            "session_evidence_refs",
            "membership_refs",
            "gross_run_pnl",
            "currency",
            "currency_authority",
            "required_cost_classes",
            "costs",
            "as_of",
            "previous_version_id",
            "previous_version_sha256",
            "known_cost_total",
            "net_after_known_costs",
            "completeness",
            "incomplete_reasons",
            "version_id",
            "record_sha256",
        }
        _require_exact_keys(payload, expected, "CampaignEconomicEvidenceVersion")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise CostEvidenceError("unsupported campaign economic evidence schema_version")
        currency_authority_raw = payload["currency_authority"]
        session_raw = _require_list(payload["session_evidence_refs"], "session_evidence_refs")
        membership_raw = _require_list(payload["membership_refs"], "membership_refs")
        required_raw = _require_list(payload["required_cost_classes"], "required_cost_classes")
        costs_raw = _require_list(payload["costs"], "costs")
        reasons_raw = _require_list(payload["incomplete_reasons"], "incomplete_reasons")
        net_raw = payload["net_after_known_costs"]
        item = cls(
            campaign_sha256=_require_string(payload["campaign_sha256"], "campaign_sha256"),
            session_evidence_refs=tuple(
                AuthorityRef.from_dict(_require_mapping(v, "session evidence ref")) for v in session_raw
            ),
            membership_refs=tuple(
                MembershipRef.from_dict(_require_mapping(v, "membership ref")) for v in membership_raw
            ),
            gross_run_pnl=_parse_decimal(payload["gross_run_pnl"], "gross_run_pnl"),
            currency=_optional_string(payload["currency"], "currency"),
            currency_authority=(
                None
                if currency_authority_raw is None
                else AuthorityRef.from_dict(_require_mapping(currency_authority_raw, "currency_authority"))
            ),
            required_cost_classes=tuple(
                CostClass(_require_string(v, "required cost class")) for v in required_raw
            ),
            costs=tuple(CostEvidence.from_dict(_require_mapping(v, "cost evidence")) for v in costs_raw),
            as_of=_parse_datetime(payload["as_of"], "as_of"),
            previous_version_id=_optional_string(payload["previous_version_id"], "previous_version_id"),
            previous_version_sha256=_optional_string(
                payload["previous_version_sha256"], "previous_version_sha256"
            ),
            known_cost_total=_parse_decimal(payload["known_cost_total"], "known_cost_total"),
            net_after_known_costs=(
                None if net_raw is None else _parse_decimal(net_raw, "net_after_known_costs")
            ),
            completeness=EconomicCompleteness(
                _require_string(payload["completeness"], "completeness")
            ),
            incomplete_reasons=tuple(_require_string(v, "incomplete reason") for v in reasons_raw),
        )
        stored_id = _require_string(payload["version_id"], "version_id")
        stored_digest = _require_string(payload["record_sha256"], "record_sha256")
        _require_sha256(stored_id, "version_id")
        _require_sha256(stored_digest, "record_sha256")
        if stored_id != item.version_id or stored_digest != item.record_sha256:
            raise CostEvidenceError("campaign economic evidence digest mismatch")
        return item


def derive_campaign_economics(
    *,
    campaign_sha256: str,
    session_evidence_refs: Sequence[AuthorityRef],
    membership_refs: Sequence[MembershipRef],
    gross_run_pnl: Decimal,
    currency: str | None,
    currency_authority: AuthorityRef | None,
    required_cost_classes: Sequence[CostClass],
    costs: Sequence[CostEvidence],
    as_of: datetime,
    previous: CampaignEconomicEvidenceVersion | None = None,
) -> CampaignEconomicEvidenceVersion:
    """Derive one immutable, append-only campaign economic evidence version.

    `gross_run_pnl` is intentionally supplied by the already-authoritative
    campaign/run evidence path. This module never recalculates betting P&L and
    never upgrades estimates into incurred cost truth.
    """

    _require_sha256(campaign_sha256, "campaign_sha256")
    _require_decimal(gross_run_pnl, "gross_run_pnl")
    _require_utc(as_of, "as_of")
    if currency is not None and not _CURRENCY_RE.fullmatch(currency):
        raise CostEvidenceError("campaign currency must be an uppercase ISO-like three-letter code")
    if (currency is None) != (currency_authority is None):
        raise CostEvidenceError("currency and currency_authority must be present together")

    session_refs = tuple(sorted(session_evidence_refs))
    memberships = tuple(sorted(membership_refs))
    required = tuple(sorted(set(required_cost_classes), key=lambda item: item.value))
    cost_items = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))
    _require_sorted_unique(session_refs, "session_evidence_refs")
    _require_sorted_unique(memberships, "membership_refs")

    allowed_memberships = set(memberships)
    source_keys: set[tuple[str, str, str]] = set()
    cost_ids: set[str] = set()
    for cost in cost_items:
        if cost.campaign_sha256 != campaign_sha256:
            raise CostEvidenceError("cost evidence belongs to a different campaign")
        if cost.available_at > as_of:
            raise CostEvidenceError("future-available cost evidence cannot be backdated into this version")
        if not set(cost.memberships).issubset(allowed_memberships):
            raise CostEvidenceError("cost evidence contains membership outside the finalized campaign")
        if cost.cost_evidence_id in cost_ids:
            raise CostEvidenceError("duplicate cost evidence id")
        cost_ids.add(cost.cost_evidence_id)
        source_key = (cost.source.family, cost.source.evidence_id, cost.source.sha256)
        if source_key in source_keys:
            raise CostEvidenceError("same immutable source cost evidence cannot be reused twice")
        source_keys.add(source_key)

    if previous is not None:
        if previous.campaign_sha256 != campaign_sha256:
            raise CostEvidenceError("successor version cannot cross campaign identity")
        if previous.session_evidence_refs != session_refs:
            raise CostEvidenceError("successor version cannot rewrite finalized session membership")
        if previous.membership_refs != memberships:
            raise CostEvidenceError("successor version cannot rewrite finalized campaign memberships")
        if previous.gross_run_pnl != gross_run_pnl:
            raise CostEvidenceError("cost correction cannot rewrite authoritative gross run P&L")
        if previous.currency != currency or previous.currency_authority != currency_authority:
            raise CostEvidenceError("cost correction cannot silently rewrite campaign currency authority")
        if previous.required_cost_classes != required:
            raise CostEvidenceError("cost correction cannot rewrite the required cost-class policy")
        if as_of < previous.as_of:
            raise CostEvidenceError("successor economic evidence cannot move as_of backwards")

    reasons: set[str] = set()
    estimated = False
    if currency is None:
        reasons.add("MISSING_CAMPAIGN_CURRENCY_AUTHORITY")

    known_total = Decimal("0")
    by_class: dict[CostClass, list[CostEvidence]] = {item: [] for item in required}
    for cost in cost_items:
        if cost.cost_class in by_class:
            by_class[cost.cost_class].append(cost)

        if cost.truth in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT}:
            if cost.basis in {CostBasis.CONFIGURED_ESTIMATE, CostBasis.SYNTHETIC_ESTIMATE}:
                estimated = True
            if cost.treatment is CostTreatment.INFORMATIONAL:
                if cost.cost_class in required:
                    reasons.add(f"INFORMATIONAL_ONLY:{cost.cost_class.value}")
                continue
            if cost.unit is not CostUnit.MONEY:
                if cost.cost_class in required:
                    reasons.add(f"NON_MONEY_UNIT:{cost.cost_class.value}")
                continue
            if currency is None or cost.currency is None:
                if cost.cost_class in required:
                    reasons.add(f"UNRESOLVED_CURRENCY:{cost.cost_class.value}")
                continue
            if cost.currency != currency:
                if cost.cost_class in required:
                    reasons.add(f"CROSS_CURRENCY:{cost.cost_class.value}:{cost.currency}")
                continue
            if cost.treatment is CostTreatment.SUBTRACT_FROM_GROSS:
                known_total += cost.amount or Decimal("0")
            # EMBEDDED_IN_GROSS is intentionally not subtracted a second time.

    for cost_class in required:
        evidence = by_class[cost_class]
        if not evidence:
            reasons.add(f"MISSING_COST_CLASS:{cost_class.value}")
            continue
        if any(item.truth is CostTruth.UNKNOWN_UNPROVEN for item in evidence):
            reasons.add(f"UNRESOLVED_COST_CLASS:{cost_class.value}")
            continue
        applicable = [item for item in evidence if item.truth is not CostTruth.NOT_APPLICABLE]
        if not applicable:
            # Every NOT_APPLICABLE item has already been validated as an
            # authoritative declaration.
            continue
        if not any(item.truth in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT} for item in applicable):
            reasons.add(f"NO_KNOWN_COST:{cost_class.value}")

    net_after_known: Decimal | None
    if currency is None:
        net_after_known = None
    else:
        net_after_known = gross_run_pnl - known_total

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
        required_cost_classes=required,
        costs=cost_items,
        as_of=as_of,
        previous_version_id=None if previous is None else previous.version_id,
        previous_version_sha256=None if previous is None else previous.record_sha256,
        known_cost_total=known_total,
        net_after_known_costs=net_after_known,
        completeness=completeness,
        incomplete_reasons=tuple(sorted(reasons)),
    )


class CampaignEconomicEvidenceStore:
    """Small append-only sidecar keyed by exact finalized campaign digest.

    Versions are immutable files. `head.json` is only a convenience pointer;
    every read re-verifies the full record digest and `verify_chain()` walks the
    append-only predecessor chain. A non-blocking OS file lock serializes one
    campaign publication without sleeps or background daemons.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def append(self, version: CampaignEconomicEvidenceVersion) -> str:
        campaign_dir = self._campaign_dir(version.campaign_sha256)
        versions_dir = campaign_dir / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        campaign_dir.mkdir(parents=True, exist_ok=True)

        with _exclusive_file_lock(campaign_dir / ".publish.lock"):
            current = self.latest(version.campaign_sha256)
            if current is None:
                if version.previous_version_id is not None:
                    raise CostStoreError("first stored version cannot name a predecessor")
            else:
                if version.version_id == current.version_id:
                    return version.version_id
                if version.previous_version_id != current.version_id:
                    raise CostStoreError("economic evidence successor does not extend the current head")
                if version.previous_version_sha256 != current.record_sha256:
                    raise CostStoreError("economic evidence predecessor digest mismatch")

            record_path = versions_dir / f"{version.version_id}.json"
            record_bytes = _canonical_json_bytes(version.to_dict())
            if record_path.exists():
                existing = record_path.read_bytes()
                if existing != record_bytes:
                    raise CostStoreError("existing immutable economic version conflicts with retry")
            else:
                _create_immutable_file(record_path, record_bytes)

            head = {
                "schema_version": SCHEMA_VERSION,
                "campaign_sha256": version.campaign_sha256,
                "version_id": version.version_id,
                "record_sha256": version.record_sha256,
            }
            _atomic_replace_json(campaign_dir / "head.json", head)
            return version.version_id

    def latest(self, campaign_sha256: str) -> CampaignEconomicEvidenceVersion | None:
        _require_sha256(campaign_sha256, "campaign_sha256")
        head_path = self._campaign_dir(campaign_sha256) / "head.json"
        if not head_path.exists():
            return None
        payload = _strict_json_bytes(head_path.read_bytes(), "economic head")
        _require_exact_keys(
            payload,
            {"schema_version", "campaign_sha256", "version_id", "record_sha256"},
            "economic head",
        )
        if payload["schema_version"] != SCHEMA_VERSION:
            raise CostStoreError("unsupported economic head schema_version")
        if payload["campaign_sha256"] != campaign_sha256:
            raise CostStoreError("economic head campaign identity mismatch")
        version_id = _require_string(payload["version_id"], "head version_id")
        digest = _require_string(payload["record_sha256"], "head record_sha256")
        if version_id != digest:
            raise CostStoreError("economic head digest mismatch")
        version = self.load(campaign_sha256, version_id)
        if version.record_sha256 != digest:
            raise CostStoreError("economic head does not match immutable version")
        return version

    def load(self, campaign_sha256: str, version_id: str) -> CampaignEconomicEvidenceVersion:
        _require_sha256(campaign_sha256, "campaign_sha256")
        _require_sha256(version_id, "version_id")
        path = self._campaign_dir(campaign_sha256) / "versions" / f"{version_id}.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise CostStoreError("economic evidence version is missing") from exc
        payload = _strict_json_bytes(raw, "economic version")
        try:
            version = CampaignEconomicEvidenceVersion.from_dict(payload)
        except (CostEvidenceError, ValueError, TypeError) as exc:
            raise CostStoreError("economic evidence version failed integrity validation") from exc
        if version.campaign_sha256 != campaign_sha256 or version.version_id != version_id:
            raise CostStoreError("economic evidence path identity mismatch")
        return version

    def verify_chain(self, campaign_sha256: str) -> tuple[CampaignEconomicEvidenceVersion, ...]:
        latest = self.latest(campaign_sha256)
        if latest is None:
            return ()
        reverse_chain: list[CampaignEconomicEvidenceVersion] = []
        seen: set[str] = set()
        current = latest
        while True:
            if current.version_id in seen:
                raise CostStoreError("economic evidence chain contains a cycle")
            seen.add(current.version_id)
            reverse_chain.append(current)
            if current.previous_version_id is None:
                break
            previous = self.load(campaign_sha256, current.previous_version_id)
            if current.previous_version_sha256 != previous.record_sha256:
                raise CostStoreError("economic evidence predecessor digest mismatch")
            current = previous
        reverse_chain.reverse()
        return tuple(reverse_chain)

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


def _require_sorted_unique(
    values: Sequence[Any],
    label: str,
    *,
    key: Any | None = None,
) -> None:
    if len(set(values)) != len(values):
        raise CostEvidenceError(f"duplicate {label}")
    expected = tuple(sorted(values, key=key)) if key is not None else tuple(sorted(values))
    if tuple(values) != expected:
        raise CostEvidenceError(f"{label} must be sorted deterministically")


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload.keys())
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CostEvidenceError(f"{label} keys mismatch: missing={missing}, extra={extra}")


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CostEvidenceError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, label)


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
    return format(value.normalize(), "f")


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
    _require_utc(parsed, label)
    if _datetime_text(parsed) != text:
        raise CostEvidenceError(f"{label} is not in canonical datetime form")
    return parsed


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


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
            parse_constant=lambda value: (_ for _ in ()).throw(
                CostStoreError(f"{label} contains non-standard numeric constant {value}")
            ),
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CostStoreError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CostStoreError(f"{label} must contain a JSON object")
    return payload


def _create_immutable_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
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
    content = _canonical_json_bytes(payload)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
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
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
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
