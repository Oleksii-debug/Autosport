from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping, Protocol

from .campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostEvidence,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
)
from .campaign_economic_authority import FinalizedCampaignAuthority


SCHEMA_VERSION = 1
_SOURCE_PREFIX = "economics.monetary-source.v1"
_ALLOCATION_PREFIX = "economics.monetary-allocation.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class MonetaryAuthorityError(ValueError):
    pass


class MonetarySourceClass(StrEnum):
    PROVIDER_BILLING = "PROVIDER_BILLING"
    COMPUTE_BILLING = "COMPUTE_BILLING"
    EXECUTION_SLIPPAGE = "EXECUTION_SLIPPAGE"
    EXECUTION_RECEIPT = "EXECUTION_RECEIPT"
    FIXED_ADMIN = "FIXED_ADMIN"


class MonetaryEvidenceQuality(StrEnum):
    INCURRED = "INCURRED"
    ESTIMATE = "ESTIMATE"
    SIMULATED = "SIMULATED"


_COST_CLASS = {
    MonetarySourceClass.PROVIDER_BILLING: CostClass.PROVIDER_DATA,
    MonetarySourceClass.COMPUTE_BILLING: CostClass.MODEL_COMPUTE_AI,
    MonetarySourceClass.EXECUTION_SLIPPAGE: CostClass.EXECUTION_SLIPPAGE,
    MonetarySourceClass.EXECUTION_RECEIPT: CostClass.EXECUTION_FEES_COMMISSION_TAX,
    MonetarySourceClass.FIXED_ADMIN: CostClass.FIXED_CAMPAIGN,
}
_TREATMENT = {
    **{value: CostTreatment.SUBTRACT_FROM_GROSS for value in MonetarySourceClass},
    MonetarySourceClass.EXECUTION_SLIPPAGE: CostTreatment.EMBEDDED_IN_GROSS,
}


@dataclass(frozen=True, slots=True)
class MonetarySourceSnapshot:
    authority_id: str
    evidence_id: str
    content_sha256: str
    amount: Decimal
    currency: str
    campaign_ids: tuple[str, ...]
    coverage_start: datetime
    coverage_end: datetime
    observed_at: datetime
    available_at: datetime
    provenance: str
    quality: MonetaryEvidenceQuality
    supersedes_evidence_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.authority_id, "authority_id")
        _text(self.evidence_id, "evidence_id")
        _sha(self.content_sha256, "content_sha256")
        _amount(self.amount)
        if not _CURRENCY.fullmatch(self.currency):
            raise MonetaryAuthorityError("currency must be uppercase three-letter code")
        _sorted_text(self.campaign_ids, "campaign_ids")
        if not self.campaign_ids:
            raise MonetaryAuthorityError("campaign_ids must be non-empty")
        for value, name in ((self.coverage_start, "coverage_start"), (self.coverage_end, "coverage_end"), (self.observed_at, "observed_at"), (self.available_at, "available_at")):
            _utc(value, name)
        if self.coverage_start > self.coverage_end or self.coverage_end > self.available_at:
            raise MonetaryAuthorityError("invalid monetary coverage interval")
        if self.observed_at > self.available_at:
            raise MonetaryAuthorityError("observed_at cannot follow available_at")
        _text(self.provenance, "provenance")
        if type(self.quality) is not MonetaryEvidenceQuality:
            raise MonetaryAuthorityError("quality must be MonetaryEvidenceQuality")
        if self.supersedes_evidence_id is not None:
            _text(self.supersedes_evidence_id, "supersedes_evidence_id")
            if self.supersedes_evidence_id == self.evidence_id:
                raise MonetaryAuthorityError("source cannot supersede itself")

    def payload(self) -> dict[str, object]:
        return {
            "authority_id": self.authority_id,
            "evidence_id": self.evidence_id,
            "content_sha256": self.content_sha256,
            "amount": _decimal(self.amount),
            "currency": self.currency,
            "campaign_ids": list(self.campaign_ids),
            "coverage_start": _dt(self.coverage_start),
            "coverage_end": _dt(self.coverage_end),
            "observed_at": _dt(self.observed_at),
            "available_at": _dt(self.available_at),
            "provenance": self.provenance,
            "quality": self.quality.value,
            "supersedes_evidence_id": self.supersedes_evidence_id,
        }


@dataclass(frozen=True, slots=True)
class MonetarySourceRecord:
    source_class: MonetarySourceClass
    snapshot: MonetarySourceSnapshot

    @property
    def sha256(self) -> str:
        return _digest({"schema_version": SCHEMA_VERSION, "source_class": self.source_class.value, "snapshot": self.snapshot.payload()})

    @property
    def ref(self) -> CostSourceRef:
        return CostSourceRef(
            family=f"{_SOURCE_PREFIX}:{self.source_class.value}:{self.snapshot.authority_id}",
            evidence_id=self.snapshot.evidence_id,
            sha256=self.sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {"source_class": self.source_class.value, "snapshot": self.snapshot.payload(), "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class SharedAllocationSnapshot:
    authority_id: str
    evidence_id: str
    content_sha256: str
    source_ref: CostSourceRef
    shares: tuple[tuple[str, Decimal], ...]
    observed_at: datetime
    available_at: datetime
    provenance: str

    def __post_init__(self) -> None:
        _text(self.authority_id, "authority_id")
        _text(self.evidence_id, "evidence_id")
        _sha(self.content_sha256, "content_sha256")
        normalized = tuple(sorted(self.shares, key=lambda item: item[0]))
        if self.shares != normalized or len({key for key, _ in self.shares}) != len(self.shares):
            raise MonetaryAuthorityError("allocation shares must be sorted and unique")
        for campaign_id, share in self.shares:
            _text(campaign_id, "campaign_id")
            _amount(share)
            if share <= 0:
                raise MonetaryAuthorityError("allocation share must be > 0")
        if not self.shares or sum((share for _, share in self.shares), Decimal("0")) != Decimal("1"):
            raise MonetaryAuthorityError("allocation must conserve exactly one source amount")
        _utc(self.observed_at, "observed_at")
        _utc(self.available_at, "available_at")
        if self.observed_at > self.available_at:
            raise MonetaryAuthorityError("allocation observed_at cannot follow available_at")
        _text(self.provenance, "provenance")

    @property
    def sha256(self) -> str:
        return _digest(self.payload())

    @property
    def ref(self) -> CostSourceRef:
        return CostSourceRef(f"{_ALLOCATION_PREFIX}:{self.authority_id}", self.evidence_id, self.sha256)

    def payload(self) -> dict[str, object]:
        return {
            "authority_id": self.authority_id,
            "evidence_id": self.evidence_id,
            "content_sha256": self.content_sha256,
            "source_ref": self.source_ref.to_dict(),
            "shares": [[key, _decimal(value)] for key, value in self.shares],
            "observed_at": _dt(self.observed_at),
            "available_at": _dt(self.available_at),
            "provenance": self.provenance,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.payload(), "sha256": self.sha256}


class MonetarySourceResolver(Protocol):
    def resolve(self, locator: str, *, as_of: datetime) -> MonetarySourceSnapshot | None: ...


class SharedAllocationResolver(Protocol):
    def resolve(self, locator: str, *, as_of: datetime) -> SharedAllocationSnapshot | None: ...


class MonetaryCostAuthority:
    """Product-composed immutable incurred-money resolver and durable store."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        source_resolvers: Mapping[MonetarySourceClass, MonetarySourceResolver],
        allocation_resolver: SharedAllocationResolver | None = None,
    ) -> None:
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self._resolvers = dict(source_resolvers)
        self._allocation_resolver = allocation_resolver
        self._sources: dict[tuple[MonetarySourceClass, str, str], MonetarySourceRecord] = {}
        self._allocations: dict[str, SharedAllocationSnapshot] = {}
        self._load()

    def capture_source(self, source_class: MonetarySourceClass, locator: str, *, as_of: datetime) -> CostSourceRef:
        _utc(as_of, "as_of")
        _text(locator, "locator")
        resolver = self._resolvers.get(source_class)
        if resolver is None:
            raise MonetaryAuthorityError("no product resolver configured for source class")
        snapshot = resolver.resolve(locator, as_of=as_of)
        if type(snapshot) is not MonetarySourceSnapshot:
            raise MonetaryAuthorityError("resolver must return exact MonetarySourceSnapshot")
        if snapshot.available_at > as_of:
            raise MonetaryAuthorityError("future-available source cannot be captured")
        record = MonetarySourceRecord(source_class, snapshot)
        self._append_source(record)
        return record.ref

    def capture_allocation(self, locator: str, *, as_of: datetime) -> CostSourceRef:
        _utc(as_of, "as_of")
        _text(locator, "locator")
        if self._allocation_resolver is None:
            raise MonetaryAuthorityError("no product allocation resolver configured")
        allocation = self._allocation_resolver.resolve(locator, as_of=as_of)
        if type(allocation) is not SharedAllocationSnapshot:
            raise MonetaryAuthorityError("resolver must return exact SharedAllocationSnapshot")
        if allocation.available_at > as_of:
            raise MonetaryAuthorityError("future-available allocation cannot be captured")
        source = self._resolve(allocation.source_ref)
        if tuple(key for key, _ in allocation.shares) != source.snapshot.campaign_ids:
            raise MonetaryAuthorityError("allocation must cover source campaigns exactly")
        conflicts = [value for value in self._allocations.values() if value.source_ref == allocation.source_ref]
        if conflicts and conflicts[0] != allocation:
            raise MonetaryAuthorityError("immutable source cannot have competing allocations")
        self._allocations.setdefault(allocation.sha256, allocation)
        self._persist()
        return allocation.ref

    def resolve_source(self, source_ref: CostSourceRef, *, as_of: datetime) -> MonetarySourceRecord:
        _utc(as_of, "as_of")
        record = self._resolve(source_ref)
        if record.snapshot.available_at > as_of:
            raise MonetaryAuthorityError("source unavailable at requested as-of")
        if self._superseder(record, as_of) is not None:
            raise MonetaryAuthorityError("source has visible append-only correction")
        return record

    def issue_cost_evidence(self, *, campaign: FinalizedCampaignAuthority, source_ref: CostSourceRef, as_of: datetime) -> CostEvidence:
        if type(campaign) is not FinalizedCampaignAuthority:
            raise MonetaryAuthorityError("campaign must be exact FinalizedCampaignAuthority")
        _utc(as_of, "as_of")
        return self._issue(campaign, source_ref, as_of, allow_superseded=False)

    def verify_cost_evidence(self, *, campaign: FinalizedCampaignAuthority, evidence: CostEvidence, as_of: datetime) -> bool:
        if type(evidence) is not CostEvidence:
            return False
        try:
            return self.issue_cost_evidence(campaign=campaign, source_ref=evidence.source, as_of=as_of) == evidence
        except (MonetaryAuthorityError, ValueError):
            return False

    def _issue(self, campaign: FinalizedCampaignAuthority, source_ref: CostSourceRef, as_of: datetime, *, allow_superseded: bool) -> CostEvidence:
        record = self._resolve(source_ref)
        item = record.snapshot
        if item.available_at > as_of:
            raise MonetaryAuthorityError("source unavailable at requested as-of")
        if not allow_superseded and self._superseder(record, as_of) is not None:
            raise MonetaryAuthorityError("source has visible append-only correction")
        if item.quality is not MonetaryEvidenceQuality.INCURRED:
            raise MonetaryAuthorityError("estimate/simulation cannot mint incurred monetary truth")
        projection = campaign.projection()
        if projection.campaign_id not in item.campaign_ids:
            raise MonetaryAuthorityError("source does not apply to finalized campaign")
        amount = item.amount
        allocation_ref = None
        shared = len(item.campaign_ids) > 1
        if shared:
            allocations = [value for value in self._allocations.values() if value.source_ref == source_ref and value.available_at <= as_of]
            if len(allocations) != 1:
                raise MonetaryAuthorityError("shared source requires one exact visible allocation")
            allocation = allocations[0]
            amount *= dict(allocation.shares)[projection.campaign_id]
            allocation_ref = allocation.ref
        supersedes: tuple[str, ...] = ()
        if item.supersedes_evidence_id is not None:
            predecessor = self._sources[(record.source_class, item.authority_id, item.supersedes_evidence_id)]
            old = self._issue(campaign, predecessor.ref, as_of, allow_superseded=True)
            supersedes = (old.cost_evidence_id,)
        return CostEvidence(
            cost_class=_COST_CLASS[record.source_class],
            truth=CostTruth.KNOWN_ZERO if amount == 0 else CostTruth.KNOWN_AMOUNT,
            basis=CostBasis.OBSERVED_INCURRED,
            treatment=_TREATMENT[record.source_class],
            source=source_ref,
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
            unit=CostUnit.MONEY,
            currency=item.currency,
            amount=amount,
            observed_at=item.observed_at,
            available_at=item.available_at,
            incurred_at=item.coverage_end,
            shared_source=shared,
            allocation_source=allocation_ref,
            supersedes_cost_evidence_ids=supersedes,
        )

    def _append_source(self, record: MonetarySourceRecord) -> None:
        item = record.snapshot
        key = (record.source_class, item.authority_id, item.evidence_id)
        previous = self._sources.get(key)
        if previous is not None:
            if previous != record:
                raise MonetaryAuthorityError("immutable source identity changed")
            return
        if item.supersedes_evidence_id is not None:
            predecessor = self._sources.get((record.source_class, item.authority_id, item.supersedes_evidence_id))
            if predecessor is None:
                raise MonetaryAuthorityError("correction predecessor missing")
            old = predecessor.snapshot
            if (old.currency, old.campaign_ids, old.coverage_start, old.coverage_end) != (item.currency, item.campaign_ids, item.coverage_start, item.coverage_end):
                raise MonetaryAuthorityError("correction cannot rewrite currency or applicability")
            if any(value.snapshot.supersedes_evidence_id == item.supersedes_evidence_id for value in self._sources.values()):
                raise MonetaryAuthorityError("correction lineage cannot branch")
        self._sources[key] = record
        self._persist()

    def _resolve(self, source_ref: CostSourceRef) -> MonetarySourceRecord:
        parts = source_ref.family.split(":")
        if len(parts) != 3 or parts[0] != _SOURCE_PREFIX:
            raise MonetaryAuthorityError("not a monetary source authority ref")
        try:
            source_class = MonetarySourceClass(parts[1])
        except ValueError as exc:
            raise MonetaryAuthorityError("unknown monetary source class") from exc
        record = self._sources.get((source_class, parts[2], source_ref.evidence_id))
        if record is None or record.sha256 != source_ref.sha256:
            raise MonetaryAuthorityError("monetary source ID+digest does not resolve exactly")
        return record

    def _superseder(self, record: MonetarySourceRecord, as_of: datetime) -> MonetarySourceRecord | None:
        values = [value for value in self._sources.values() if value.source_class is record.source_class and value.snapshot.authority_id == record.snapshot.authority_id and value.snapshot.supersedes_evidence_id == record.snapshot.evidence_id and value.snapshot.available_at <= as_of]
        if len(values) > 1:
            raise MonetaryAuthorityError("correction lineage branches")
        return values[0] if values else None

    def _persist(self) -> None:
        _atomic_json(self.root / "monetary-cost-authority.json", {
            "schema_version": SCHEMA_VERSION,
            "sources": [value.to_dict() for value in sorted(self._sources.values(), key=lambda value: value.sha256)],
            "allocations": [value.to_dict() for value in sorted(self._allocations.values(), key=lambda value: value.sha256)],
        })

    def _load(self) -> None:
        path = self.root / "monetary-cost-authority.json"
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MonetaryAuthorityError("monetary authority store unreadable") from exc
        if set(raw) != {"schema_version", "sources", "allocations"} or raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryAuthorityError("invalid monetary authority store schema")
        for value in raw["sources"]:
            record = _source_from_dict(value)
            key = (record.source_class, record.snapshot.authority_id, record.snapshot.evidence_id)
            if key in self._sources:
                raise MonetaryAuthorityError("duplicate durable monetary source")
            self._sources[key] = record
        for record in self._sources.values():
            predecessor = record.snapshot.supersedes_evidence_id
            if predecessor is not None and (record.source_class, record.snapshot.authority_id, predecessor) not in self._sources:
                raise MonetaryAuthorityError("durable correction predecessor missing")
        for value in raw["allocations"]:
            allocation = _allocation_from_dict(value)
            self._resolve(allocation.source_ref)
            if any(old.source_ref == allocation.source_ref and old != allocation for old in self._allocations.values()):
                raise MonetaryAuthorityError("durable source has competing allocations")
            self._allocations[allocation.sha256] = allocation


def _source_from_dict(raw: dict[str, object]) -> MonetarySourceRecord:
    snap = raw["snapshot"]
    if not isinstance(snap, dict):
        raise MonetaryAuthorityError("source snapshot must be object")
    item = MonetarySourceSnapshot(
        authority_id=str(snap["authority_id"]), evidence_id=str(snap["evidence_id"]), content_sha256=str(snap["content_sha256"]),
        amount=Decimal(str(snap["amount"])), currency=str(snap["currency"]), campaign_ids=tuple(snap["campaign_ids"]),
        coverage_start=_parse_dt(snap["coverage_start"]), coverage_end=_parse_dt(snap["coverage_end"]), observed_at=_parse_dt(snap["observed_at"]), available_at=_parse_dt(snap["available_at"]),
        provenance=str(snap["provenance"]), quality=MonetaryEvidenceQuality(str(snap["quality"])), supersedes_evidence_id=snap["supersedes_evidence_id"] if isinstance(snap["supersedes_evidence_id"], str) else None,
    )
    record = MonetarySourceRecord(MonetarySourceClass(str(raw["source_class"])), item)
    if raw.get("sha256") != record.sha256:
        raise MonetaryAuthorityError("durable monetary source digest mismatch")
    return record


def _allocation_from_dict(raw: dict[str, object]) -> SharedAllocationSnapshot:
    source = raw["source_ref"]
    if not isinstance(source, dict):
        raise MonetaryAuthorityError("allocation source_ref must be object")
    shares = raw["shares"]
    if not isinstance(shares, list):
        raise MonetaryAuthorityError("allocation shares must be array")
    item = SharedAllocationSnapshot(
        authority_id=str(raw["authority_id"]), evidence_id=str(raw["evidence_id"]), content_sha256=str(raw["content_sha256"]),
        source_ref=CostSourceRef.from_dict(source), shares=tuple((str(v[0]), Decimal(str(v[1]))) for v in shares),
        observed_at=_parse_dt(raw["observed_at"]), available_at=_parse_dt(raw["available_at"]), provenance=str(raw["provenance"]),
    )
    if raw.get("sha256") != item.sha256:
        raise MonetaryAuthorityError("durable allocation digest mismatch")
    return item


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MonetaryAuthorityError(f"{label} must be non-empty canonical text")


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise MonetaryAuthorityError(f"{label} must be lowercase SHA-256")


def _amount(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise MonetaryAuthorityError("amount/share must be finite non-negative Decimal")


def _sorted_text(values: tuple[str, ...], label: str) -> None:
    for value in values:
        _text(value, label)
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise MonetaryAuthorityError(f"{label} must be sorted and unique")


def _utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise MonetaryAuthorityError(f"{label} must be UTC")


def _decimal(value: Decimal) -> str:
    _amount(value)
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _dt(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat().replace("+00:00", "Z")


def _parse_dt(value: object) -> datetime:
    if not isinstance(value, str):
        raise MonetaryAuthorityError("datetime must be string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _utc(parsed, "datetime")
    if _dt(parsed) != value:
        raise MonetaryAuthorityError("datetime must use canonical UTC encoding")
    return parsed


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
