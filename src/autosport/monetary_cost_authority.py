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
from typing import Any, Mapping, Protocol

from .campaign_cost_evidence import CostEvidence, CostSourceRef
from .campaign_economic_authority import FinalizedCampaignAuthority


SCHEMA_VERSION = 2
_SOURCE_PREFIX = "economics.monetary-source.v2"
_ALLOCATION_PREFIX = "economics.monetary-allocation.v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class MonetaryAuthorityError(ValueError):
    """Raised when monetary source evidence cannot be trusted exactly."""


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


@dataclass(frozen=True, slots=True)
class MonetarySourceSnapshot:
    """Immutable observation returned by a non-authorizing resolver.

    Generic/injected resolvers are deliberately useful only for estimates and
    simulations. ``INCURRED`` remains vocabulary for a future concrete
    product-owned adapter, but this generic schema cannot capture or reload it.
    """

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
        _identifier(self.authority_id, "authority_id")
        _text(self.evidence_id, "evidence_id")
        _sha(self.content_sha256, "content_sha256")
        _amount(self.amount, "amount")
        if not isinstance(self.currency, str) or _CURRENCY.fullmatch(self.currency) is None:
            raise MonetaryAuthorityError("currency must be uppercase three-letter code")
        _sorted_text(self.campaign_ids, "campaign_ids")
        if not self.campaign_ids:
            raise MonetaryAuthorityError("campaign_ids must be non-empty")
        for value, label in (
            (self.coverage_start, "coverage_start"),
            (self.coverage_end, "coverage_end"),
            (self.observed_at, "observed_at"),
            (self.available_at, "available_at"),
        ):
            _utc(value, label)
        if self.coverage_start > self.coverage_end:
            raise MonetaryAuthorityError("coverage_start cannot follow coverage_end")
        if self.coverage_end > self.available_at:
            raise MonetaryAuthorityError("coverage cannot end after evidence is available")
        if self.observed_at > self.available_at:
            raise MonetaryAuthorityError("observed_at cannot follow available_at")
        _text(self.provenance, "provenance")
        if type(self.quality) is not MonetaryEvidenceQuality:
            raise MonetaryAuthorityError("quality must be MonetaryEvidenceQuality")
        if self.supersedes_evidence_id is not None:
            _text(self.supersedes_evidence_id, "supersedes_evidence_id")
            if self.supersedes_evidence_id == self.evidence_id:
                raise MonetaryAuthorityError("source cannot supersede itself")

    def payload(self) -> dict[str, Any]:
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

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MonetarySourceSnapshot":
        _keys(
            raw,
            {
                "authority_id", "evidence_id", "content_sha256", "amount",
                "currency", "campaign_ids", "coverage_start", "coverage_end",
                "observed_at", "available_at", "provenance", "quality",
                "supersedes_evidence_id",
            },
            "MonetarySourceSnapshot",
        )
        try:
            quality = MonetaryEvidenceQuality(_string(raw["quality"], "quality"))
        except ValueError as exc:
            raise MonetaryAuthorityError("unknown monetary evidence quality") from exc
        return cls(
            authority_id=_string(raw["authority_id"], "authority_id"),
            evidence_id=_string(raw["evidence_id"], "evidence_id"),
            content_sha256=_string(raw["content_sha256"], "content_sha256"),
            amount=_parse_decimal(raw["amount"], "amount"),
            currency=_string(raw["currency"], "currency"),
            campaign_ids=tuple(
                _string(value, "campaign_id")
                for value in _list(raw["campaign_ids"], "campaign_ids")
            ),
            coverage_start=_parse_dt(raw["coverage_start"], "coverage_start"),
            coverage_end=_parse_dt(raw["coverage_end"], "coverage_end"),
            observed_at=_parse_dt(raw["observed_at"], "observed_at"),
            available_at=_parse_dt(raw["available_at"], "available_at"),
            provenance=_string(raw["provenance"], "provenance"),
            quality=quality,
            supersedes_evidence_id=(
                None
                if raw["supersedes_evidence_id"] is None
                else _string(raw["supersedes_evidence_id"], "supersedes_evidence_id")
            ),
        )


@dataclass(frozen=True, slots=True)
class MonetarySourceRecord:
    source_class: MonetarySourceClass
    snapshot: MonetarySourceSnapshot

    @property
    def sha256(self) -> str:
        return _digest(self.payload())

    @property
    def ref(self) -> CostSourceRef:
        return CostSourceRef(
            family=f"{_SOURCE_PREFIX}:{self.source_class.value}:{self.snapshot.authority_id}",
            evidence_id=self.snapshot.evidence_id,
            sha256=self.sha256,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_class": self.source_class.value,
            "snapshot": self.snapshot.payload(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "sha256": self.sha256}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MonetarySourceRecord":
        _keys(
            raw,
            {"schema_version", "source_class", "snapshot", "sha256"},
            "MonetarySourceRecord",
        )
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryAuthorityError("unsupported monetary source record schema")
        try:
            source_class = MonetarySourceClass(
                _string(raw["source_class"], "source_class")
            )
        except ValueError as exc:
            raise MonetaryAuthorityError("unknown monetary source class") from exc
        item = cls(
            source_class=source_class,
            snapshot=MonetarySourceSnapshot.from_dict(
                _mapping(raw["snapshot"], "snapshot")
            ),
        )
        if _string(raw["sha256"], "sha256") != item.sha256:
            raise MonetaryAuthorityError("durable monetary source digest mismatch")
        return item


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
        _identifier(self.authority_id, "authority_id")
        _text(self.evidence_id, "evidence_id")
        _sha(self.content_sha256, "content_sha256")
        if type(self.source_ref) is not CostSourceRef:
            raise MonetaryAuthorityError("source_ref must be exact CostSourceRef")
        if tuple(sorted(self.shares, key=lambda value: value[0])) != self.shares:
            raise MonetaryAuthorityError("allocation shares must be sorted")
        if len({campaign_id for campaign_id, _ in self.shares}) != len(self.shares):
            raise MonetaryAuthorityError("allocation campaign_ids must be unique")
        for campaign_id, share in self.shares:
            _text(campaign_id, "allocation campaign_id")
            _amount(share, "allocation share")
            if share <= 0:
                raise MonetaryAuthorityError("allocation shares must be positive")
        if (
            not self.shares
            or sum((share for _, share in self.shares), Decimal("0"))
            != Decimal("1")
        ):
            raise MonetaryAuthorityError(
                "allocation shares must conserve exactly one source amount"
            )
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
        return CostSourceRef(
            family=f"{_ALLOCATION_PREFIX}:{self.authority_id}",
            evidence_id=self.evidence_id,
            sha256=self.sha256,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "authority_id": self.authority_id,
            "evidence_id": self.evidence_id,
            "content_sha256": self.content_sha256,
            "source_ref": self.source_ref.to_dict(),
            "shares": [
                [campaign_id, _decimal(share)]
                for campaign_id, share in self.shares
            ],
            "observed_at": _dt(self.observed_at),
            "available_at": _dt(self.available_at),
            "provenance": self.provenance,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "sha256": self.sha256}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SharedAllocationSnapshot":
        _keys(
            raw,
            {
                "schema_version", "authority_id", "evidence_id", "content_sha256",
                "source_ref", "shares", "observed_at", "available_at",
                "provenance", "sha256",
            },
            "SharedAllocationSnapshot",
        )
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MonetaryAuthorityError("unsupported allocation schema")
        shares: list[tuple[str, Decimal]] = []
        for value in _list(raw["shares"], "shares"):
            if not isinstance(value, list) or len(value) != 2:
                raise MonetaryAuthorityError(
                    "allocation entry must be [campaign_id, share]"
                )
            shares.append(
                (
                    _string(value[0], "campaign_id"),
                    _parse_decimal(value[1], "share"),
                )
            )
        item = cls(
            authority_id=_string(raw["authority_id"], "authority_id"),
            evidence_id=_string(raw["evidence_id"], "evidence_id"),
            content_sha256=_string(raw["content_sha256"], "content_sha256"),
            source_ref=CostSourceRef.from_dict(
                _mapping(raw["source_ref"], "source_ref")
            ),
            shares=tuple(shares),
            observed_at=_parse_dt(raw["observed_at"], "observed_at"),
            available_at=_parse_dt(raw["available_at"], "available_at"),
            provenance=_string(raw["provenance"], "provenance"),
        )
        if _string(raw["sha256"], "sha256") != item.sha256:
            raise MonetaryAuthorityError("durable allocation digest mismatch")
        return item


class MonetarySourceResolver(Protocol):
    def resolve(
        self,
        locator: str,
        *,
        as_of: datetime,
    ) -> MonetarySourceSnapshot | None: ...


class SharedAllocationResolver(Protocol):
    def resolve(
        self,
        locator: str,
        *,
        as_of: datetime,
    ) -> SharedAllocationSnapshot | None: ...


class MonetaryCostAuthority:
    """Fail-closed cache for non-authoritative monetary observations.

    No injected resolver in schema v2 can mint incurred monetary truth. A later
    positive path must be a concrete product-owned adapter to an existing
    provider/billing/compute/execution/admin authority and must re-resolve that
    authority on restart. Until then, this class deliberately keeps #645
    economic completeness closed.
    """

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
        self._sources: dict[
            tuple[MonetarySourceClass, str, str], MonetarySourceRecord
        ] = {}
        self._allocations: dict[str, SharedAllocationSnapshot] = {}
        self._load()

    def capture_source(
        self,
        source_class: MonetarySourceClass,
        locator: str,
        *,
        as_of: datetime,
    ) -> CostSourceRef:
        if type(source_class) is not MonetarySourceClass:
            raise MonetaryAuthorityError(
                "source_class must be MonetarySourceClass"
            )
        _utc(as_of, "as_of")
        _text(locator, "locator")
        resolver = self._resolvers.get(source_class)
        if resolver is None:
            raise MonetaryAuthorityError("no resolver configured for source class")
        snapshot = resolver.resolve(locator, as_of=as_of)
        if type(snapshot) is not MonetarySourceSnapshot:
            raise MonetaryAuthorityError(
                "resolver must return exact MonetarySourceSnapshot"
            )
        if snapshot.available_at > as_of:
            raise MonetaryAuthorityError(
                "future-available source cannot be captured"
            )
        if snapshot.quality is MonetaryEvidenceQuality.INCURRED:
            raise MonetaryAuthorityError(
                "injected resolver cannot mint incurred monetary truth"
            )
        record = MonetarySourceRecord(
            source_class=source_class,
            snapshot=snapshot,
        )
        self._append_source(record)
        return record.ref

    def capture_allocation(
        self,
        locator: str,
        *,
        as_of: datetime,
    ) -> CostSourceRef:
        _utc(as_of, "as_of")
        _text(locator, "locator")
        if self._allocation_resolver is None:
            raise MonetaryAuthorityError("no allocation resolver configured")
        allocation = self._allocation_resolver.resolve(locator, as_of=as_of)
        if type(allocation) is not SharedAllocationSnapshot:
            raise MonetaryAuthorityError(
                "allocation resolver returned wrong type"
            )
        if allocation.available_at > as_of:
            raise MonetaryAuthorityError(
                "future-available allocation cannot be captured"
            )
        source = self._resolve(allocation.source_ref)
        if (
            tuple(campaign_id for campaign_id, _ in allocation.shares)
            != source.snapshot.campaign_ids
        ):
            raise MonetaryAuthorityError(
                "allocation must cover source campaigns exactly"
            )
        if any(
            previous.source_ref == allocation.source_ref
            and previous != allocation
            for previous in self._allocations.values()
        ):
            raise MonetaryAuthorityError(
                "source cannot have competing allocations"
            )
        self._allocations.setdefault(allocation.sha256, allocation)
        self._persist()
        return allocation.ref

    def resolve_source(
        self,
        source_ref: CostSourceRef,
        *,
        as_of: datetime,
    ) -> MonetarySourceRecord:
        _utc(as_of, "as_of")
        record = self._resolve(source_ref)
        if record.snapshot.available_at > as_of:
            raise MonetaryAuthorityError(
                "source unavailable at requested as-of"
            )
        if self._superseder(record, as_of) is not None:
            raise MonetaryAuthorityError(
                "source has visible append-only correction"
            )
        return record

    def issue_cost_evidence(
        self,
        *,
        campaign: FinalizedCampaignAuthority,
        source_ref: CostSourceRef,
        as_of: datetime,
    ) -> CostEvidence:
        if type(campaign) is not FinalizedCampaignAuthority:
            raise MonetaryAuthorityError(
                "campaign must be exact FinalizedCampaignAuthority"
            )
        _utc(as_of, "as_of")
        self.resolve_source(source_ref, as_of=as_of)
        raise MonetaryAuthorityError(
            "no product-owned incurred monetary adapter is integrated on schema v2"
        )

    def verify_cost_evidence(
        self,
        *,
        campaign: FinalizedCampaignAuthority,
        evidence: CostEvidence,
        as_of: datetime,
    ) -> bool:
        return False

    def _append_source(self, record: MonetarySourceRecord) -> None:
        item = record.snapshot
        if item.quality is MonetaryEvidenceQuality.INCURRED:
            raise MonetaryAuthorityError(
                "generic monetary cache cannot store incurred truth"
            )
        key = (record.source_class, item.authority_id, item.evidence_id)
        previous = self._sources.get(key)
        if previous is not None:
            if previous != record:
                raise MonetaryAuthorityError("immutable source identity changed")
            return

        self._sources[key] = record
        try:
            self._validate_correction_graph()
        except Exception:
            del self._sources[key]
            raise
        self._persist()

    def _validate_correction_graph(self) -> None:
        """Apply the same correction invariants to live and durable state."""

        child_by_predecessor: dict[
            tuple[MonetarySourceClass, str, str],
            tuple[MonetarySourceClass, str, str],
        ] = {}
        for key, record in self._sources.items():
            item = record.snapshot
            predecessor_id = item.supersedes_evidence_id
            if predecessor_id is None:
                continue
            predecessor_key = (
                record.source_class,
                item.authority_id,
                predecessor_id,
            )
            predecessor = self._sources.get(predecessor_key)
            if predecessor is None:
                raise MonetaryAuthorityError(
                    "durable correction predecessor missing"
                )
            old = predecessor.snapshot
            if (
                old.currency,
                old.campaign_ids,
                old.coverage_start,
                old.coverage_end,
            ) != (
                item.currency,
                item.campaign_ids,
                item.coverage_start,
                item.coverage_end,
            ):
                raise MonetaryAuthorityError(
                    "correction cannot rewrite currency or applicability"
                )
            previous_child = child_by_predecessor.get(predecessor_key)
            if previous_child is not None and previous_child != key:
                raise MonetaryAuthorityError(
                    "correction lineage cannot branch"
                )
            child_by_predecessor[predecessor_key] = key

        # A forged durable file can contain a cycle even though append order
        # cannot create one. Reject it before any tip can resolve.
        for start in self._sources:
            seen: set[tuple[MonetarySourceClass, str, str]] = set()
            current = start
            while True:
                if current in seen:
                    raise MonetaryAuthorityError(
                        "correction lineage cannot contain cycles"
                    )
                seen.add(current)
                record = self._sources[current]
                predecessor_id = record.snapshot.supersedes_evidence_id
                if predecessor_id is None:
                    break
                current = (
                    record.source_class,
                    record.snapshot.authority_id,
                    predecessor_id,
                )

    def _resolve(self, source_ref: CostSourceRef) -> MonetarySourceRecord:
        if type(source_ref) is not CostSourceRef:
            raise MonetaryAuthorityError(
                "source_ref must be exact CostSourceRef"
            )
        parts = source_ref.family.split(":")
        if len(parts) != 3 or parts[0] != _SOURCE_PREFIX:
            raise MonetaryAuthorityError(
                "not a schema-v2 monetary source ref"
            )
        try:
            source_class = MonetarySourceClass(parts[1])
        except ValueError as exc:
            raise MonetaryAuthorityError(
                "unknown monetary source class"
            ) from exc
        record = self._sources.get(
            (source_class, parts[2], source_ref.evidence_id)
        )
        if record is None or record.sha256 != source_ref.sha256:
            raise MonetaryAuthorityError(
                "monetary source ID+digest does not resolve exactly"
            )
        return record

    def _superseder(
        self,
        record: MonetarySourceRecord,
        as_of: datetime,
    ) -> MonetarySourceRecord | None:
        candidates = [
            value
            for value in self._sources.values()
            if value.source_class is record.source_class
            and value.snapshot.authority_id == record.snapshot.authority_id
            and value.snapshot.supersedes_evidence_id
            == record.snapshot.evidence_id
            and value.snapshot.available_at <= as_of
        ]
        if len(candidates) > 1:
            # This should already be impossible after graph validation; keep a
            # local guard so corrupted in-memory state also fails closed.
            raise MonetaryAuthorityError("correction lineage branches")
        return candidates[0] if candidates else None

    def _persist(self) -> None:
        _atomic_json(
            self.root / "monetary-cost-authority.json",
            {
                "schema_version": SCHEMA_VERSION,
                "authoritative_incurred": False,
                "sources": [
                    value.to_dict()
                    for value in sorted(
                        self._sources.values(),
                        key=lambda value: value.sha256,
                    )
                ],
                "allocations": [
                    value.to_dict()
                    for value in sorted(
                        self._allocations.values(),
                        key=lambda value: value.sha256,
                    )
                ],
            },
        )

    def _load(self) -> None:
        path = self.root / "monetary-cost-authority.json"
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MonetaryAuthorityError(
                "monetary authority cache is unreadable"
            ) from exc
        data = _mapping(raw, "store")
        _keys(
            data,
            {
                "schema_version",
                "authoritative_incurred",
                "sources",
                "allocations",
            },
            "store",
        )
        if data["schema_version"] != SCHEMA_VERSION:
            raise MonetaryAuthorityError(
                "unsupported monetary authority cache schema"
            )
        if data["authoritative_incurred"] is not False:
            raise MonetaryAuthorityError(
                "generic cache cannot claim incurred authority"
            )
        for value in _list(data["sources"], "sources"):
            record = MonetarySourceRecord.from_dict(
                _mapping(value, "source")
            )
            if record.snapshot.quality is MonetaryEvidenceQuality.INCURRED:
                raise MonetaryAuthorityError(
                    "durable generic cache cannot contain incurred monetary truth"
                )
            key = (
                record.source_class,
                record.snapshot.authority_id,
                record.snapshot.evidence_id,
            )
            if key in self._sources:
                raise MonetaryAuthorityError(
                    "duplicate durable monetary source"
                )
            self._sources[key] = record

        # Restart must accept exactly the same correction graph that live
        # append accepts. Validate the whole graph before allocations or any
        # source resolution can use a forged branch/cycle.
        self._validate_correction_graph()

        for value in _list(data["allocations"], "allocations"):
            allocation = SharedAllocationSnapshot.from_dict(
                _mapping(value, "allocation")
            )
            self._resolve(allocation.source_ref)
            if allocation.sha256 in self._allocations:
                raise MonetaryAuthorityError(
                    "duplicate durable allocation"
                )
            if any(
                old.source_ref == allocation.source_ref
                and old != allocation
                for old in self._allocations.values()
            ):
                raise MonetaryAuthorityError(
                    "durable source has competing allocations"
                )
            self._allocations[allocation.sha256] = allocation


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MonetaryAuthorityError(
            f"{label} must be non-empty canonical text"
        )


def _identifier(value: str, label: str) -> None:
    _text(value, label)
    if ":" in value:
        raise MonetaryAuthorityError(f"{label} cannot contain ':'")


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise MonetaryAuthorityError(
            f"{label} must be lowercase SHA-256"
        )


def _amount(value: Decimal, label: str) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
    ):
        raise MonetaryAuthorityError(
            f"{label} must be finite non-negative Decimal"
        )


def _sorted_text(values: tuple[str, ...], label: str) -> None:
    for value in values:
        _text(value, label)
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise MonetaryAuthorityError(
            f"{label} must be sorted and unique"
        )


def _utc(value: datetime, label: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise MonetaryAuthorityError(f"{label} must be UTC")


def _decimal(value: Decimal) -> str:
    _amount(value, "decimal")
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _parse_decimal(value: Any, label: str) -> Decimal:
    text = _string(value, label)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise MonetaryAuthorityError(
            f"{label} is not Decimal-compatible"
        ) from exc
    _amount(parsed, label)
    if _decimal(parsed) != text:
        raise MonetaryAuthorityError(
            f"{label} must use canonical decimal encoding"
        )
    return parsed


def _dt(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_dt(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if not text.endswith("Z"):
        raise MonetaryAuthorityError(
            f"{label} must use UTC Z notation"
        )
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise MonetaryAuthorityError(f"{label} is invalid") from exc
    if _dt(parsed) != text:
        raise MonetaryAuthorityError(
            f"{label} is not canonical datetime text"
        )
    return parsed


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        raise MonetaryAuthorityError(f"{label} keys mismatch")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise MonetaryAuthorityError(f"{label} must be object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise MonetaryAuthorityError(f"{label} must be array")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise MonetaryAuthorityError(f"{label} must be string")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
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
