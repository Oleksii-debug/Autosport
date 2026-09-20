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
import tempfile
from typing import Any, Mapping, Sequence

from .monotonic_workspace_authority import AuthorityPhase, MonotonicWorkspaceAuthority


SCHEMA_VERSION = 1
_SOURCE_FAMILY = "economics.monetary-allocation.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class MonetaryCostAuthorityError(RuntimeError):
    """Raised when incurred monetary evidence cannot be trusted exactly."""


class MonetarySourceClass(StrEnum):
    PROVIDER_DATA = "PROVIDER_DATA"
    MODEL_COMPUTE_AI = "MODEL_COMPUTE_AI"
    EXECUTION_FEES_COMMISSION_TAX = "EXECUTION_FEES_COMMISSION_TAX"
    FIXED_CAMPAIGN = "FIXED_CAMPAIGN"


class MonetaryEvidenceQuality(StrEnum):
    INCURRED_RECEIPT = "INCURRED_RECEIPT"
    OWNER_FIXED_EXPENSE = "OWNER_FIXED_EXPENSE"


@dataclass(frozen=True, order=True, slots=True)
class UsageEvidenceRef:
    family: str
    evidence_id: str
    sha256: str

    def __post_init__(self) -> None:
        _text(self.family, "usage family")
        _text(self.evidence_id, "usage evidence_id")
        _sha256(self.sha256, "usage sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "family": self.family,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "UsageEvidenceRef":
        _keys(raw, {"family", "evidence_id", "sha256"}, "usage ref")
        return cls(
            family=_string(raw["family"], "family"),
            evidence_id=_string(raw["evidence_id"], "evidence_id"),
            sha256=_string(raw["sha256"], "sha256"),
        )


@dataclass(frozen=True, slots=True)
class IncurredMonetaryReceipt:
    source_class: MonetarySourceClass
    source_family: str
    source_authority_id: str
    source_evidence_id: str
    source_sha256: str
    amount: Decimal
    currency: str
    incurred_from: datetime
    incurred_to: datetime
    observed_at: datetime
    available_at: datetime
    quality: MonetaryEvidenceQuality
    supersedes_receipt_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.source_family, "source_family")
        _text(self.source_authority_id, "source_authority_id")
        _text(self.source_evidence_id, "source_evidence_id")
        _sha256(self.source_sha256, "source_sha256")
        _money(self.amount, self.currency)
        for label, value in (
            ("incurred_from", self.incurred_from),
            ("incurred_to", self.incurred_to),
            ("observed_at", self.observed_at),
            ("available_at", self.available_at),
        ):
            _utc(value, label)
        if self.incurred_from > self.incurred_to:
            raise MonetaryCostAuthorityError("incurred interval is reversed")
        if self.observed_at > self.available_at:
            raise MonetaryCostAuthorityError("observed_at cannot exceed available_at")
        if self.incurred_to > self.available_at:
            raise MonetaryCostAuthorityError("incurred evidence cannot be available before it occurred")
        if self.supersedes_receipt_id is not None:
            _sha256(self.supersedes_receipt_id, "supersedes_receipt_id")

    @property
    def receipt_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.receipt_id

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_class": self.source_class.value,
            "source_family": self.source_family,
            "source_authority_id": self.source_authority_id,
            "source_evidence_id": self.source_evidence_id,
            "source_sha256": self.source_sha256,
            "amount": _decimal_text(self.amount),
            "currency": self.currency,
            "incurred_from": _datetime_text(self.incurred_from),
            "incurred_to": _datetime_text(self.incurred_to),
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
            "quality": self.quality.value,
            "supersedes_receipt_id": self.supersedes_receipt_id,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["receipt_id"] = self.receipt_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IncurredMonetaryReceipt":
        _keys(
            raw,
            {
                "schema_version", "source_class", "source_family", "source_authority_id",
                "source_evidence_id", "source_sha256", "amount", "currency",
                "incurred_from", "incurred_to", "observed_at", "available_at", "quality",
                "supersedes_receipt_id", "receipt_id", "record_sha256",
            },
            "monetary receipt",
        )
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryCostAuthorityError("unsupported monetary receipt schema")
        item = cls(
            source_class=MonetarySourceClass(_string(raw["source_class"], "source_class")),
            source_family=_string(raw["source_family"], "source_family"),
            source_authority_id=_string(raw["source_authority_id"], "source_authority_id"),
            source_evidence_id=_string(raw["source_evidence_id"], "source_evidence_id"),
            source_sha256=_string(raw["source_sha256"], "source_sha256"),
            amount=_parse_decimal(raw["amount"], "amount"),
            currency=_string(raw["currency"], "currency"),
            incurred_from=_parse_datetime(raw["incurred_from"], "incurred_from"),
            incurred_to=_parse_datetime(raw["incurred_to"], "incurred_to"),
            observed_at=_parse_datetime(raw["observed_at"], "observed_at"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
            quality=MonetaryEvidenceQuality(_string(raw["quality"], "quality")),
            supersedes_receipt_id=_optional_string(raw["supersedes_receipt_id"], "supersedes_receipt_id"),
        )
        if raw["receipt_id"] != item.receipt_id or raw["record_sha256"] != item.record_sha256:
            raise MonetaryCostAuthorityError("monetary receipt digest mismatch")
        return item


@dataclass(frozen=True, slots=True)
class CampaignMonetaryAllocation:
    receipt_id: str
    receipt_sha256: str
    campaign_sha256: str
    amount: Decimal
    currency: str
    allocated_at: datetime
    usage_refs: tuple[UsageEvidenceRef, ...] = ()
    supersedes_allocation_id: str | None = None

    def __post_init__(self) -> None:
        _sha256(self.receipt_id, "receipt_id")
        _sha256(self.receipt_sha256, "receipt_sha256")
        _sha256(self.campaign_sha256, "campaign_sha256")
        _money(self.amount, self.currency)
        _utc(self.allocated_at, "allocated_at")
        if tuple(sorted(self.usage_refs)) != self.usage_refs or len(set(self.usage_refs)) != len(self.usage_refs):
            raise MonetaryCostAuthorityError("usage_refs must be sorted and unique")
        if self.supersedes_allocation_id is not None:
            _sha256(self.supersedes_allocation_id, "supersedes_allocation_id")

    @property
    def allocation_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.allocation_id

    @property
    def source_family(self) -> str:
        return _SOURCE_FAMILY

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "receipt_sha256": self.receipt_sha256,
            "campaign_sha256": self.campaign_sha256,
            "amount": _decimal_text(self.amount),
            "currency": self.currency,
            "allocated_at": _datetime_text(self.allocated_at),
            "usage_refs": [value.to_dict() for value in self.usage_refs],
            "supersedes_allocation_id": self.supersedes_allocation_id,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["allocation_id"] = self.allocation_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CampaignMonetaryAllocation":
        _keys(
            raw,
            {
                "schema_version", "receipt_id", "receipt_sha256", "campaign_sha256",
                "amount", "currency", "allocated_at", "usage_refs",
                "supersedes_allocation_id", "allocation_id", "record_sha256",
            },
            "monetary allocation",
        )
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryCostAuthorityError("unsupported monetary allocation schema")
        item = cls(
            receipt_id=_string(raw["receipt_id"], "receipt_id"),
            receipt_sha256=_string(raw["receipt_sha256"], "receipt_sha256"),
            campaign_sha256=_string(raw["campaign_sha256"], "campaign_sha256"),
            amount=_parse_decimal(raw["amount"], "amount"),
            currency=_string(raw["currency"], "currency"),
            allocated_at=_parse_datetime(raw["allocated_at"], "allocated_at"),
            usage_refs=tuple(
                UsageEvidenceRef.from_dict(_mapping(value, "usage_ref"))
                for value in _list(raw["usage_refs"], "usage_refs")
            ),
            supersedes_allocation_id=_optional_string(raw["supersedes_allocation_id"], "supersedes_allocation_id"),
        )
        if raw["allocation_id"] != item.allocation_id or raw["record_sha256"] != item.record_sha256:
            raise MonetaryCostAuthorityError("monetary allocation digest mismatch")
        return item


@dataclass(frozen=True, slots=True)
class ResolvedMonetaryCost:
    allocation_id: str
    source_class: MonetarySourceClass
    amount: Decimal
    currency: str
    incurred_at: datetime
    observed_at: datetime
    available_at: datetime
    source_authority_id: str
    source_evidence_id: str
    source_sha256: str


class MonetaryCostAuthorityStore:
    """Append-only incurred-money resolver consumed by campaign economics.

    The store never converts currencies and never treats public prices, configured
    estimates, provider credits, token counts, or simulations as incurred money.
    Source receipts must identify an immutable external/admin authority and exact
    source digest. Shared receipt attribution is explicit and conserves the source
    amount. The workspace-external monotonic journal fences rollback of this local
    evidence registry; it is not itself source-origin proof.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        authority_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.root,
            domain="autosport.monetary_cost_authority.v1",
            key="global",
            authority_root=authority_root,
        )

    def append_receipt(self, receipt: IncurredMonetaryReceipt) -> str:
        if type(receipt) is not IncurredMonetaryReceipt:
            raise MonetaryCostAuthorityError("receipt must be canonical IncurredMonetaryReceipt")
        receipts, allocations = self._load_state()
        by_id = {value.receipt_id: value for value in receipts}
        if receipt.receipt_id in by_id:
            if by_id[receipt.receipt_id] != receipt:
                raise MonetaryCostAuthorityError("receipt identity collision")
            return receipt.receipt_id
        if receipt.supersedes_receipt_id is not None:
            prior = by_id.get(receipt.supersedes_receipt_id)
            if prior is None:
                raise MonetaryCostAuthorityError("receipt correction must supersede committed receipt")
            if prior.source_class is not receipt.source_class:
                raise MonetaryCostAuthorityError("receipt correction cannot cross source class")
            if (
                prior.source_family != receipt.source_family
                or prior.source_authority_id != receipt.source_authority_id
                or prior.currency != receipt.currency
            ):
                raise MonetaryCostAuthorityError("receipt correction cannot rewrite source authority or currency")
            if any(value.supersedes_receipt_id == prior.receipt_id for value in receipts):
                raise MonetaryCostAuthorityError("receipt already has a correction successor")
        self._publish(tuple(sorted((*receipts, receipt), key=lambda value: value.receipt_id)), allocations)
        return receipt.receipt_id

    def append_allocation(self, allocation: CampaignMonetaryAllocation) -> str:
        if type(allocation) is not CampaignMonetaryAllocation:
            raise MonetaryCostAuthorityError("allocation must be canonical CampaignMonetaryAllocation")
        receipts, allocations = self._load_state()
        receipt = _by_receipt_id(receipts, allocation.receipt_id)
        if allocation.receipt_sha256 != receipt.record_sha256:
            raise MonetaryCostAuthorityError("allocation receipt digest mismatch")
        if allocation.currency != receipt.currency:
            raise MonetaryCostAuthorityError("allocation cannot convert receipt currency")
        if allocation.allocated_at < receipt.available_at:
            raise MonetaryCostAuthorityError("allocation cannot predate source availability")
        by_id = {value.allocation_id: value for value in allocations}
        if allocation.allocation_id in by_id:
            if by_id[allocation.allocation_id] != allocation:
                raise MonetaryCostAuthorityError("allocation identity collision")
            return allocation.allocation_id
        if allocation.supersedes_allocation_id is not None:
            prior = by_id.get(allocation.supersedes_allocation_id)
            if prior is None:
                raise MonetaryCostAuthorityError("allocation correction must supersede committed allocation")
            if prior.receipt_id != allocation.receipt_id or prior.campaign_sha256 != allocation.campaign_sha256:
                raise MonetaryCostAuthorityError("allocation correction cannot rewrite receipt or campaign")
            if any(value.supersedes_allocation_id == prior.allocation_id for value in allocations):
                raise MonetaryCostAuthorityError("allocation already has a correction successor")
        candidate = tuple(sorted((*allocations, allocation), key=lambda value: value.allocation_id))
        _validate_allocations(receipts, candidate)
        self._publish(receipts, candidate)
        return allocation.allocation_id

    def resolve_cost_source(
        self,
        *,
        family: str,
        evidence_id: str,
        sha256: str,
        campaign_sha256: str,
        as_of: datetime,
    ) -> ResolvedMonetaryCost:
        if family != _SOURCE_FAMILY:
            raise MonetaryCostAuthorityError("cost source is not a canonical monetary allocation")
        _sha256(evidence_id, "evidence_id")
        _sha256(sha256, "sha256")
        _sha256(campaign_sha256, "campaign_sha256")
        _utc(as_of, "as_of")
        receipts, allocations = self._load_state()
        allocation = _by_allocation_id(allocations, evidence_id)
        if allocation.record_sha256 != sha256:
            raise MonetaryCostAuthorityError("allocation source digest mismatch")
        if allocation.campaign_sha256 != campaign_sha256:
            raise MonetaryCostAuthorityError("allocation belongs to a different campaign")
        if allocation.allocated_at > as_of:
            raise MonetaryCostAuthorityError("future-available allocation cannot be backdated")
        for successor in allocations:
            if successor.supersedes_allocation_id == allocation.allocation_id and successor.allocated_at <= as_of:
                raise MonetaryCostAuthorityError("allocation was superseded before as_of")
        receipt = _by_receipt_id(receipts, allocation.receipt_id)
        if receipt.record_sha256 != allocation.receipt_sha256:
            raise MonetaryCostAuthorityError("resolved receipt digest mismatch")
        if receipt.available_at > as_of:
            raise MonetaryCostAuthorityError("future-available receipt cannot be backdated")
        for successor in receipts:
            if successor.supersedes_receipt_id == receipt.receipt_id and successor.available_at <= as_of:
                raise MonetaryCostAuthorityError("receipt was superseded before as_of")
        return ResolvedMonetaryCost(
            allocation_id=allocation.allocation_id,
            source_class=receipt.source_class,
            amount=allocation.amount,
            currency=allocation.currency,
            incurred_at=receipt.incurred_to,
            observed_at=receipt.observed_at,
            available_at=max(receipt.available_at, allocation.allocated_at),
            source_authority_id=receipt.source_authority_id,
            source_evidence_id=receipt.source_evidence_id,
            source_sha256=receipt.source_sha256,
        )

    def verify(self) -> tuple[tuple[IncurredMonetaryReceipt, ...], tuple[CampaignMonetaryAllocation, ...]]:
        return self._load_state()

    def _load_state(self) -> tuple[tuple[IncurredMonetaryReceipt, ...], tuple[CampaignMonetaryAllocation, ...]]:
        path = self._state_path()
        if not path.exists():
            self._authority.recover(observed_state_sha256=None)
            return (), ()
        raw = _strict_json(path.read_bytes(), "monetary authority state")
        _keys(raw, {"schema_version", "receipts", "allocations", "state_sha256"}, "monetary authority state")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryCostAuthorityError("unsupported monetary authority state schema")
        receipts = tuple(
            IncurredMonetaryReceipt.from_dict(_mapping(value, "receipt"))
            for value in _list(raw["receipts"], "receipts")
        )
        allocations = tuple(
            CampaignMonetaryAllocation.from_dict(_mapping(value, "allocation"))
            for value in _list(raw["allocations"], "allocations")
        )
        if tuple(sorted(receipts, key=lambda value: value.receipt_id)) != receipts:
            raise MonetaryCostAuthorityError("receipts are not canonically sorted")
        if tuple(sorted(allocations, key=lambda value: value.allocation_id)) != allocations:
            raise MonetaryCostAuthorityError("allocations are not canonically sorted")
        _validate_receipt_lineage(receipts)
        _validate_allocations(receipts, allocations)
        expected = _state_digest(receipts, allocations)
        if raw["state_sha256"] != expected:
            raise MonetaryCostAuthorityError("monetary authority state digest mismatch")
        self._authority.recover(observed_state_sha256=expected, tx_id=expected, semantic_binding_sha256=_binding(expected))
        return receipts, allocations

    def _publish(
        self,
        receipts: tuple[IncurredMonetaryReceipt, ...],
        allocations: tuple[CampaignMonetaryAllocation, ...],
    ) -> None:
        _validate_receipt_lineage(receipts)
        _validate_allocations(receipts, allocations)
        previous_receipts, previous_allocations = self._load_state()
        previous = None if not self._state_path().exists() else _state_digest(previous_receipts, previous_allocations)
        intended = _state_digest(receipts, allocations)
        if intended == previous:
            return
        binding = _binding(intended)
        prepared = self._authority.prepare(
            tx_id=intended,
            observed_state_sha256=previous,
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )
        if prepared.phase is not AuthorityPhase.PREPARE or prepared.intended_state_sha256 != intended:
            raise MonetaryCostAuthorityError("monetary state did not acquire exact PREPARE")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "receipts": [value.to_dict() for value in receipts],
            "allocations": [value.to_dict() for value in allocations],
            "state_sha256": intended,
        }
        _atomic_json(self._state_path(), payload)
        observed = _strict_json(self._state_path().read_bytes(), "monetary authority state")
        if observed.get("state_sha256") != intended:
            raise MonetaryCostAuthorityError("monetary state failed durable re-read")
        self._authority.commit(
            tx_id=intended,
            observed_state_sha256=intended,
            semantic_binding_sha256=binding,
        )

    def _state_path(self) -> Path:
        return self.root / "monetary_cost_authority" / "state.json"


def _validate_receipt_lineage(receipts: Sequence[IncurredMonetaryReceipt]) -> None:
    by_id: dict[str, IncurredMonetaryReceipt] = {}
    superseded: set[str] = set()
    source_keys: set[tuple[str, str, str]] = set()
    for receipt in receipts:
        if receipt.receipt_id in by_id:
            raise MonetaryCostAuthorityError("duplicate receipt identity")
        source_key = (receipt.source_family, receipt.source_evidence_id, receipt.source_sha256)
        if source_key in source_keys:
            raise MonetaryCostAuthorityError("same immutable source receipt was registered twice")
        source_keys.add(source_key)
        if receipt.supersedes_receipt_id is not None:
            prior = by_id.get(receipt.supersedes_receipt_id)
            if prior is None:
                raise MonetaryCostAuthorityError("receipt correction predecessor must appear first")
            if prior.receipt_id in superseded:
                raise MonetaryCostAuthorityError("receipt has multiple correction successors")
            superseded.add(prior.receipt_id)
        by_id[receipt.receipt_id] = receipt


def _validate_allocations(
    receipts: Sequence[IncurredMonetaryReceipt],
    allocations: Sequence[CampaignMonetaryAllocation],
) -> None:
    receipts_by_id = {value.receipt_id: value for value in receipts}
    allocations_by_id: dict[str, CampaignMonetaryAllocation] = {}
    superseded: set[str] = set()
    usage_owners: dict[UsageEvidenceRef, str] = {}
    effective: list[CampaignMonetaryAllocation] = []
    for allocation in allocations:
        receipt = receipts_by_id.get(allocation.receipt_id)
        if receipt is None or allocation.receipt_sha256 != receipt.record_sha256:
            raise MonetaryCostAuthorityError("allocation references unknown/tampered receipt")
        if allocation.currency != receipt.currency:
            raise MonetaryCostAuthorityError("allocation currency differs from receipt")
        if allocation.supersedes_allocation_id is not None:
            prior = allocations_by_id.get(allocation.supersedes_allocation_id)
            if prior is None:
                raise MonetaryCostAuthorityError("allocation correction predecessor must appear first")
            if prior.allocation_id in superseded:
                raise MonetaryCostAuthorityError("allocation has multiple correction successors")
            superseded.add(prior.allocation_id)
        allocations_by_id[allocation.allocation_id] = allocation
    effective = [value for value in allocations if value.allocation_id not in superseded]
    totals: dict[str, Decimal] = {}
    for allocation in effective:
        totals[allocation.receipt_id] = totals.get(allocation.receipt_id, Decimal("0")) + allocation.amount
        for usage_ref in allocation.usage_refs:
            owner = usage_owners.get(usage_ref)
            if owner is not None and owner != allocation.allocation_id:
                raise MonetaryCostAuthorityError("usage evidence was allocated more than once")
            usage_owners[usage_ref] = allocation.allocation_id
    for receipt_id, total in totals.items():
        receipt = receipts_by_id[receipt_id]
        if total > receipt.amount:
            raise MonetaryCostAuthorityError("allocations exceed authoritative receipt amount")


def _by_receipt_id(receipts: Sequence[IncurredMonetaryReceipt], receipt_id: str) -> IncurredMonetaryReceipt:
    matches = [value for value in receipts if value.receipt_id == receipt_id]
    if len(matches) != 1:
        raise MonetaryCostAuthorityError("receipt is not uniquely committed")
    return matches[0]


def _by_allocation_id(allocations: Sequence[CampaignMonetaryAllocation], allocation_id: str) -> CampaignMonetaryAllocation:
    matches = [value for value in allocations if value.allocation_id == allocation_id]
    if len(matches) != 1:
        raise MonetaryCostAuthorityError("allocation is not uniquely committed")
    return matches[0]


def monetary_allocation_source_family() -> str:
    return _SOURCE_FAMILY


def _state_digest(
    receipts: Sequence[IncurredMonetaryReceipt],
    allocations: Sequence[CampaignMonetaryAllocation],
) -> str:
    return _digest({
        "schema_version": SCHEMA_VERSION,
        "receipts": [value.to_dict() for value in receipts],
        "allocations": [value.to_dict() for value in allocations],
    })


def _binding(state_sha256: str) -> str:
    return _digest({"domain": "autosport.monetary_cost_authority.v1", "state_sha256": state_sha256})


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MonetaryCostAuthorityError(f"{label} must be a non-empty canonical string")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MonetaryCostAuthorityError(f"{label} must be lowercase SHA-256 hex")


def _utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MonetaryCostAuthorityError(f"{label} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise MonetaryCostAuthorityError(f"{label} must be UTC")


def _money(amount: Decimal, currency: str) -> None:
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        raise MonetaryCostAuthorityError("money amount must be a finite non-negative Decimal")
    if not isinstance(currency, str) or _CURRENCY_RE.fullmatch(currency) is None:
        raise MonetaryCostAuthorityError("currency must be uppercase three-letter ISO-style code")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise MonetaryCostAuthorityError("decimal must be finite")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _parse_decimal(value: Any, label: str) -> Decimal:
    text = _string(value, label)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise MonetaryCostAuthorityError(f"{label} is not Decimal-compatible") from exc
    if _decimal_text(parsed) != text:
        raise MonetaryCostAuthorityError(f"{label} is not canonical Decimal text")
    return parsed


def _datetime_text(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if not text.endswith("Z"):
        raise MonetaryCostAuthorityError(f"{label} must use UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise MonetaryCostAuthorityError(f"{label} is invalid") from exc
    if _datetime_text(parsed) != text:
        raise MonetaryCostAuthorityError(f"{label} is not canonical")
    return parsed


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        raise MonetaryCostAuthorityError(f"{label} keys mismatch")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise MonetaryCostAuthorityError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise MonetaryCostAuthorityError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise MonetaryCostAuthorityError(f"{label} must be an array")
    return value


def _strict_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MonetaryCostAuthorityError(f"{label} is not UTF-8") from exc

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MonetaryCostAuthorityError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=lambda item: (_ for _ in ()).throw(MonetaryCostAuthorityError(f"non-standard number {item}")))
    except json.JSONDecodeError as exc:
        raise MonetaryCostAuthorityError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise MonetaryCostAuthorityError(f"{label} must contain an object")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((_canonical_json(payload) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
