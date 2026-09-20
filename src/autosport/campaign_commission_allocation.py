from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from .betfair_market_commission_authority import (
    SOURCE_FAMILY as BETFAIR_COMMISSION_SOURCE_FAMILY,
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionReceipt,
)
from .campaign_economic_authority import (
    CanonicalMembershipRef,
    FinalizedCampaignAuthority,
)
from .monotonic_workspace_authority import AuthorityPhase, MonotonicWorkspaceAuthority


SCHEMA_VERSION = 1
ALLOCATION_FAMILY = "betfair.market-commission-campaign-allocation.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CampaignCommissionAllocationError(RuntimeError):
    """Raised when Betfair commission cannot be allocated to campaign authority."""


@dataclass(frozen=True, order=True, slots=True)
class CampaignCommissionAllocation:
    source_receipt_id: str
    source_receipt_sha256: str
    source_account_id: str
    market_id: str
    source_amount: Decimal
    campaign_id: str
    campaign_sha256: str
    memberships: tuple[CanonicalMembershipRef, ...]
    allocated_amount: Decimal
    currency: str
    incurred_at: datetime
    observed_at: datetime
    available_at: datetime
    supersedes_allocation_id: str | None = None

    def __post_init__(self) -> None:
        _sha256(self.source_receipt_id, "source_receipt_id")
        _sha256(self.source_receipt_sha256, "source_receipt_sha256")
        _text(self.source_account_id, "source_account_id")
        _text(self.market_id, "market_id")
        _money(self.source_amount, "source_amount")
        _text(self.campaign_id, "campaign_id")
        _sha256(self.campaign_sha256, "campaign_sha256")
        if not self.memberships:
            raise CampaignCommissionAllocationError(
                "campaign allocation requires canonical campaign memberships"
            )
        _sorted_unique(self.memberships, "memberships")
        _money(self.allocated_amount, "allocated_amount")
        if self.allocated_amount > self.source_amount:
            raise CampaignCommissionAllocationError(
                "allocated_amount cannot exceed source_amount"
            )
        if _CURRENCY_RE.fullmatch(self.currency) is None:
            raise CampaignCommissionAllocationError(
                "currency must be uppercase three-letter code"
            )
        for label, value in (
            ("incurred_at", self.incurred_at),
            ("observed_at", self.observed_at),
            ("available_at", self.available_at),
        ):
            _utc(value, label)
        if self.incurred_at > self.available_at or self.observed_at > self.available_at:
            raise CampaignCommissionAllocationError(
                "allocation cannot be available before source evidence"
            )
        if self.supersedes_allocation_id is not None:
            _sha256(self.supersedes_allocation_id, "supersedes_allocation_id")

    @property
    def allocation_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.allocation_id

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "allocation_family": ALLOCATION_FAMILY,
            "source_family": BETFAIR_COMMISSION_SOURCE_FAMILY,
            "source_receipt_id": self.source_receipt_id,
            "source_receipt_sha256": self.source_receipt_sha256,
            "source_account_id": self.source_account_id,
            "market_id": self.market_id,
            "source_amount": _decimal_text(self.source_amount),
            "campaign_id": self.campaign_id,
            "campaign_sha256": self.campaign_sha256,
            "memberships": [_membership_dict(value) for value in self.memberships],
            "allocated_amount": _decimal_text(self.allocated_amount),
            "currency": self.currency,
            "incurred_at": _datetime_text(self.incurred_at),
            "observed_at": _datetime_text(self.observed_at),
            "available_at": _datetime_text(self.available_at),
            "supersedes_allocation_id": self.supersedes_allocation_id,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["allocation_id"] = self.allocation_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CampaignCommissionAllocation":
        _keys(
            raw,
            {
                "schema_version",
                "allocation_family",
                "source_family",
                "source_receipt_id",
                "source_receipt_sha256",
                "source_account_id",
                "market_id",
                "source_amount",
                "campaign_id",
                "campaign_sha256",
                "memberships",
                "allocated_amount",
                "currency",
                "incurred_at",
                "observed_at",
                "available_at",
                "supersedes_allocation_id",
                "allocation_id",
                "record_sha256",
            },
            "campaign commission allocation",
        )
        if (
            raw["schema_version"] != SCHEMA_VERSION
            or raw["allocation_family"] != ALLOCATION_FAMILY
            or raw["source_family"] != BETFAIR_COMMISSION_SOURCE_FAMILY
        ):
            raise CampaignCommissionAllocationError(
                "unsupported campaign commission allocation"
            )
        item = cls(
            source_receipt_id=_string(raw["source_receipt_id"], "source_receipt_id"),
            source_receipt_sha256=_string(
                raw["source_receipt_sha256"], "source_receipt_sha256"
            ),
            source_account_id=_string(raw["source_account_id"], "source_account_id"),
            market_id=_string(raw["market_id"], "market_id"),
            source_amount=_parse_decimal(raw["source_amount"], "source_amount"),
            campaign_id=_string(raw["campaign_id"], "campaign_id"),
            campaign_sha256=_string(raw["campaign_sha256"], "campaign_sha256"),
            memberships=tuple(
                _membership_from_dict(_mapping(value, "membership"))
                for value in _list(raw["memberships"], "memberships")
            ),
            allocated_amount=_parse_decimal(
                raw["allocated_amount"], "allocated_amount"
            ),
            currency=_string(raw["currency"], "currency"),
            incurred_at=_parse_datetime(raw["incurred_at"], "incurred_at"),
            observed_at=_parse_datetime(raw["observed_at"], "observed_at"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
            supersedes_allocation_id=_optional_string(
                raw["supersedes_allocation_id"], "supersedes_allocation_id"
            ),
        )
        if (
            raw["allocation_id"] != item.allocation_id
            or raw["record_sha256"] != item.record_sha256
        ):
            raise CampaignCommissionAllocationError(
                "campaign commission allocation digest mismatch"
            )
        return item


@dataclass(frozen=True, slots=True)
class CampaignCommissionAllocationBatch:
    source_receipt_id: str
    source_receipt_sha256: str
    allocations: tuple[CampaignCommissionAllocation, ...]
    supersedes_batch_id: str | None = None

    def __post_init__(self) -> None:
        _sha256(self.source_receipt_id, "source_receipt_id")
        _sha256(self.source_receipt_sha256, "source_receipt_sha256")
        if not self.allocations:
            raise CampaignCommissionAllocationError(
                "allocation batch must contain at least one campaign"
            )
        _sorted_unique(
            self.allocations,
            "allocations",
            key=lambda value: value.campaign_sha256,
        )
        if any(
            value.source_receipt_id != self.source_receipt_id
            or value.source_receipt_sha256 != self.source_receipt_sha256
            for value in self.allocations
        ):
            raise CampaignCommissionAllocationError(
                "allocation batch mixes source receipts"
            )
        first = self.allocations[0]
        if any(
            value.source_amount != first.source_amount
            or value.currency != first.currency
            or value.market_id != first.market_id
            or value.source_account_id != first.source_account_id
            for value in self.allocations
        ):
            raise CampaignCommissionAllocationError(
                "allocation batch mixes source monetary identity"
            )
        total = sum(
            (value.allocated_amount for value in self.allocations),
            Decimal("0"),
        )
        if total != first.source_amount:
            raise CampaignCommissionAllocationError(
                "campaign allocations must conserve the exact source amount"
            )
        campaign_ids = {value.campaign_sha256 for value in self.allocations}
        if len(campaign_ids) != len(self.allocations):
            raise CampaignCommissionAllocationError(
                "one source batch cannot allocate twice to the same campaign"
            )
        if self.supersedes_batch_id is not None:
            _sha256(self.supersedes_batch_id, "supersedes_batch_id")

    @property
    def batch_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.batch_id

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "allocation_family": ALLOCATION_FAMILY,
            "source_receipt_id": self.source_receipt_id,
            "source_receipt_sha256": self.source_receipt_sha256,
            "allocations": [value.to_dict() for value in self.allocations],
            "supersedes_batch_id": self.supersedes_batch_id,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["batch_id"] = self.batch_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "CampaignCommissionAllocationBatch":
        _keys(
            raw,
            {
                "schema_version",
                "allocation_family",
                "source_receipt_id",
                "source_receipt_sha256",
                "allocations",
                "supersedes_batch_id",
                "batch_id",
                "record_sha256",
            },
            "campaign commission allocation batch",
        )
        if (
            raw["schema_version"] != SCHEMA_VERSION
            or raw["allocation_family"] != ALLOCATION_FAMILY
        ):
            raise CampaignCommissionAllocationError(
                "unsupported campaign commission allocation batch"
            )
        item = cls(
            source_receipt_id=_string(raw["source_receipt_id"], "source_receipt_id"),
            source_receipt_sha256=_string(
                raw["source_receipt_sha256"], "source_receipt_sha256"
            ),
            allocations=tuple(
                CampaignCommissionAllocation.from_dict(_mapping(value, "allocation"))
                for value in _list(raw["allocations"], "allocations")
            ),
            supersedes_batch_id=_optional_string(
                raw["supersedes_batch_id"], "supersedes_batch_id"
            ),
        )
        if raw["batch_id"] != item.batch_id or raw["record_sha256"] != item.record_sha256:
            raise CampaignCommissionAllocationError("allocation batch digest mismatch")
        return item


class BetfairCommissionCampaignAllocationAuthority:
    """Bind authenticated Betfair commission to exact finalized campaign authority.

    The Betfair source amount/currency/time/origin are never accepted from callers.
    The only caller-selected economic decision is the explicit split of one exact
    source receipt across already-finalized campaign authorities. Publication is
    atomic at the receipt level and requires exact conservation, so a receipt cannot
    be double-counted by racing independent campaign writes.

    Durable allocation bytes plus the generic monotonic journal are integrity and
    rollback evidence only. Positive resolve authority is regained after restart
    only by re-resolving the exact provider receipt and re-presenting the exact
    finalized campaign allocation split.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        source: BetfairMarketCommissionAuthority,
        *,
        authority_root: str | os.PathLike[str] | None = None,
    ) -> None:
        if type(source) is not BetfairMarketCommissionAuthority:
            raise TypeError("source must be BetfairMarketCommissionAuthority")
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self._source = source
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.root,
            domain="autosport.betfair_campaign_commission_allocation.v1",
            key="betfair:market-commission",
            authority_root=authority_root,
        )
        self._issued_batch_ids: set[str] = set()

    def allocate_receipt(
        self,
        *,
        receipt_id: str,
        record_sha256: str,
        as_of: datetime,
        targets: Sequence[tuple[FinalizedCampaignAuthority, Decimal]],
    ) -> tuple[CampaignCommissionAllocation, ...]:
        _utc(as_of, "as_of")
        receipt = self._source.resolve(
            receipt_id=receipt_id,
            record_sha256=record_sha256,
            as_of=as_of,
        )
        target_items = tuple(targets)
        if not target_items:
            raise CampaignCommissionAllocationError(
                "commission allocation requires at least one finalized campaign"
            )

        projections: list[tuple[Any, Decimal]] = []
        for campaign, amount in target_items:
            if type(campaign) is not FinalizedCampaignAuthority:
                raise CampaignCommissionAllocationError(
                    "allocation target requires exact FinalizedCampaignAuthority"
                )
            projection = campaign.projection()
            _money(amount, "allocated amount")
            projections.append((projection, amount))
        projections.sort(key=lambda value: value[0].campaign_sha256)
        if len({value[0].campaign_sha256 for value in projections}) != len(projections):
            raise CampaignCommissionAllocationError(
                "source receipt cannot target the same campaign twice"
            )
        if sum((value[1] for value in projections), Decimal("0")) != receipt.commission:
            raise CampaignCommissionAllocationError(
                "campaign allocations must conserve the exact Betfair commission"
            )

        batches = self._load_batches()
        same_source = [
            value for value in batches if value.source_receipt_id == receipt.receipt_id
        ]
        if len(same_source) > 1:
            raise CampaignCommissionAllocationError(
                "source receipt has multiple allocation batches"
            )

        predecessor = self._predecessor_batch(receipt, batches)
        predecessor_by_campaign = (
            {}
            if predecessor is None
            else {
                value.campaign_sha256: value for value in predecessor.allocations
            }
        )
        if predecessor is not None:
            current_campaigns = {value[0].campaign_sha256 for value in projections}
            if current_campaigns != set(predecessor_by_campaign):
                raise CampaignCommissionAllocationError(
                    "commission correction cannot rewrite campaign applicability"
                )

        allocations = tuple(
            CampaignCommissionAllocation(
                source_receipt_id=receipt.receipt_id,
                source_receipt_sha256=receipt.record_sha256,
                source_account_id=receipt.account_id,
                market_id=receipt.market_id,
                source_amount=receipt.commission,
                campaign_id=projection.campaign_id,
                campaign_sha256=projection.campaign_sha256,
                memberships=projection.membership_refs,
                allocated_amount=amount,
                currency=receipt.currency,
                incurred_at=receipt.settled_at,
                observed_at=receipt.observed_at,
                available_at=receipt.available_at,
                supersedes_allocation_id=(
                    None
                    if predecessor is None
                    else predecessor_by_campaign[
                        projection.campaign_sha256
                    ].allocation_id
                ),
            )
            for projection, amount in projections
        )
        candidate = CampaignCommissionAllocationBatch(
            source_receipt_id=receipt.receipt_id,
            source_receipt_sha256=receipt.record_sha256,
            allocations=allocations,
            supersedes_batch_id=None if predecessor is None else predecessor.batch_id,
        )

        if same_source:
            existing = same_source[0]
            if existing != candidate:
                raise CampaignCommissionAllocationError(
                    "Betfair commission receipt is already allocated differently"
                )
            self._issued_batch_ids.add(existing.batch_id)
            return existing.allocations

        self._publish(tuple(sorted((*batches, candidate), key=lambda value: value.batch_id)))
        self._issued_batch_ids.add(candidate.batch_id)
        return candidate.allocations

    def resolve(
        self,
        *,
        allocation_id: str,
        record_sha256: str,
        as_of: datetime,
    ) -> CampaignCommissionAllocation:
        _sha256(allocation_id, "allocation_id")
        _sha256(record_sha256, "record_sha256")
        _utc(as_of, "as_of")
        batches = self._load_batches()
        matches = [
            (batch, allocation)
            for batch in batches
            for allocation in batch.allocations
            if allocation.allocation_id == allocation_id
        ]
        if len(matches) != 1:
            raise CampaignCommissionAllocationError(
                "campaign commission allocation is not uniquely committed"
            )
        batch, allocation = matches[0]
        if batch.batch_id not in self._issued_batch_ids:
            raise CampaignCommissionAllocationError(
                "campaign commission allocation requires exact source/campaign reacquisition"
            )
        if allocation.record_sha256 != record_sha256:
            raise CampaignCommissionAllocationError(
                "campaign commission allocation digest mismatch"
            )
        if allocation.available_at > as_of:
            raise CampaignCommissionAllocationError(
                "future commission allocation cannot be backdated"
            )
        self._source.resolve(
            receipt_id=allocation.source_receipt_id,
            record_sha256=allocation.source_receipt_sha256,
            as_of=as_of,
        )
        for successor in batches:
            if (
                successor.supersedes_batch_id == batch.batch_id
                and successor.allocations[0].available_at <= as_of
            ):
                raise CampaignCommissionAllocationError(
                    "campaign commission allocation was superseded before as_of"
                )
        return allocation

    def verify(self) -> tuple[CampaignCommissionAllocationBatch, ...]:
        """Validate durable integrity only; this does not mint allocation authority."""
        return self._load_batches()

    def _predecessor_batch(
        self,
        receipt: BetfairMarketCommissionReceipt,
        batches: Sequence[CampaignCommissionAllocationBatch],
    ) -> CampaignCommissionAllocationBatch | None:
        predecessor_id = receipt.supersedes_receipt_id
        if predecessor_id is None:
            return None
        matches = [value for value in batches if value.source_receipt_id == predecessor_id]
        if len(matches) > 1:
            raise CampaignCommissionAllocationError(
                "superseded source receipt has multiple allocation batches"
            )
        return None if not matches else matches[0]

    def _load_batches(self) -> tuple[CampaignCommissionAllocationBatch, ...]:
        path = self._state_path()
        if not path.exists():
            self._authority.recover(observed_state_sha256=None)
            return ()
        raw = _strict_json(path.read_bytes(), "campaign commission allocation state")
        _keys(raw, {"schema_version", "batches", "state_sha256"}, "allocation state")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise CampaignCommissionAllocationError(
                "unsupported campaign commission allocation state"
            )
        batches = tuple(
            CampaignCommissionAllocationBatch.from_dict(_mapping(value, "batch"))
            for value in _list(raw["batches"], "batches")
        )
        if tuple(sorted(batches, key=lambda value: value.batch_id)) != batches:
            raise CampaignCommissionAllocationError(
                "allocation batches are not canonically sorted"
            )
        _validate_batch_lineage(batches)
        state_sha256 = _state_digest(batches)
        if raw["state_sha256"] != state_sha256:
            raise CampaignCommissionAllocationError(
                "campaign commission allocation state digest mismatch"
            )
        self._authority.recover(
            observed_state_sha256=state_sha256,
            tx_id=state_sha256,
            semantic_binding_sha256=_binding(state_sha256),
        )
        return batches

    def _publish(
        self, batches: tuple[CampaignCommissionAllocationBatch, ...]
    ) -> None:
        _validate_batch_lineage(batches)
        previous_batches = self._load_batches()
        previous = None if not self._state_path().exists() else _state_digest(previous_batches)
        intended = _state_digest(batches)
        if previous == intended:
            return
        binding = _binding(intended)
        prepared = self._authority.prepare(
            tx_id=intended,
            observed_state_sha256=previous,
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )
        if (
            prepared.phase is not AuthorityPhase.PREPARE
            or prepared.intended_state_sha256 != intended
        ):
            raise CampaignCommissionAllocationError(
                "campaign commission allocation did not acquire exact PREPARE"
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "batches": [value.to_dict() for value in batches],
            "state_sha256": intended,
        }
        _atomic_json(self._state_path(), payload)
        observed = _strict_json(
            self._state_path().read_bytes(), "campaign commission allocation state"
        )
        if observed.get("state_sha256") != intended:
            raise CampaignCommissionAllocationError(
                "campaign commission allocation durable re-read failed"
            )
        self._authority.commit(
            tx_id=intended,
            observed_state_sha256=intended,
            semantic_binding_sha256=binding,
        )

    def _state_path(self) -> Path:
        return self.root / "campaign_commission_allocation" / "state.json"


def _validate_batch_lineage(
    batches: Sequence[CampaignCommissionAllocationBatch],
) -> None:
    by_id = {value.batch_id: value for value in batches}
    if len(by_id) != len(batches):
        raise CampaignCommissionAllocationError("duplicate allocation batch identity")
    by_receipt: dict[str, CampaignCommissionAllocationBatch] = {}
    superseded: set[str] = set()
    for batch in batches:
        if batch.source_receipt_id in by_receipt:
            raise CampaignCommissionAllocationError(
                "source receipt has duplicate allocation batch"
            )
        by_receipt[batch.source_receipt_id] = batch
        if batch.supersedes_batch_id is not None:
            predecessor = by_id.get(batch.supersedes_batch_id)
            if predecessor is None:
                raise CampaignCommissionAllocationError(
                    "allocation correction predecessor is missing"
                )
            if batch.supersedes_batch_id in superseded:
                raise CampaignCommissionAllocationError(
                    "allocation batch has multiple correction successors"
                )
            superseded.add(batch.supersedes_batch_id)
            prior_campaigns = {
                value.campaign_sha256: value for value in predecessor.allocations
            }
            current_campaigns = {
                value.campaign_sha256: value for value in batch.allocations
            }
            if set(prior_campaigns) != set(current_campaigns):
                raise CampaignCommissionAllocationError(
                    "allocation correction changed campaign applicability"
                )
            for campaign_sha256, current in current_campaigns.items():
                if (
                    current.supersedes_allocation_id
                    != prior_campaigns[campaign_sha256].allocation_id
                ):
                    raise CampaignCommissionAllocationError(
                        "allocation correction does not bind exact prior allocation"
                    )
        elif any(value.supersedes_allocation_id is not None for value in batch.allocations):
            raise CampaignCommissionAllocationError(
                "genesis allocation cannot supersede prior allocation"
            )


def _membership_dict(value: CanonicalMembershipRef) -> dict[str, str]:
    return {"kind": value.kind, "evidence_id": value.evidence_id, "sha256": value.sha256}


def _membership_from_dict(raw: Mapping[str, Any]) -> CanonicalMembershipRef:
    _keys(raw, {"kind", "evidence_id", "sha256"}, "membership")
    return CanonicalMembershipRef(
        kind=_string(raw["kind"], "kind"),
        evidence_id=_string(raw["evidence_id"], "evidence_id"),
        sha256=_string(raw["sha256"], "sha256"),
    )


def _state_digest(batches: Sequence[CampaignCommissionAllocationBatch]) -> str:
    return _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "allocation_family": ALLOCATION_FAMILY,
            "batch_ids": [value.batch_id for value in batches],
        }
    )


def _binding(state_sha256: str) -> str:
    return _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "allocation_family": ALLOCATION_FAMILY,
            "state_sha256": state_sha256,
        }
    )


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CampaignCommissionAllocationError(
            f"{label} must be a non-empty canonical string"
        )


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CampaignCommissionAllocationError(
            f"{label} must be lowercase SHA-256 hex"
        )


def _utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CampaignCommissionAllocationError(f"{label} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise CampaignCommissionAllocationError(f"{label} must be expressed in UTC")


def _money(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise CampaignCommissionAllocationError(
            f"{label} must be a finite non-negative Decimal"
        )


def _sorted_unique(
    values: Sequence[Any], label: str, *, key: Any | None = None
) -> None:
    try:
        if len(set(values)) != len(values):
            raise CampaignCommissionAllocationError(f"duplicate {label}")
    except TypeError as exc:
        raise CampaignCommissionAllocationError(
            f"{label} must contain immutable values"
        ) from exc
    expected = tuple(sorted(values, key=key)) if key is not None else tuple(sorted(values))
    if tuple(values) != expected:
        raise CampaignCommissionAllocationError(
            f"{label} must be sorted deterministically"
        )


def _keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        raise CampaignCommissionAllocationError(
            f"{label} keys mismatch: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CampaignCommissionAllocationError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CampaignCommissionAllocationError(f"{label} must be an array")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CampaignCommissionAllocationError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _decimal_text(value: Decimal) -> str:
    _money(value, "decimal")
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
    except (InvalidOperation, ValueError) as exc:
        raise CampaignCommissionAllocationError(
            f"{label} is not Decimal-compatible"
        ) from exc
    _money(parsed, label)
    if _decimal_text(parsed) != text:
        raise CampaignCommissionAllocationError(
            f"{label} is not canonical decimal text"
        )
    return parsed


def _datetime_text(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if not text.endswith("Z"):
        raise CampaignCommissionAllocationError(f"{label} must use UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise CampaignCommissionAllocationError(f"{label} is invalid") from exc
    if _datetime_text(parsed) != text:
        raise CampaignCommissionAllocationError(
            f"{label} is not canonical datetime text"
        )
    return parsed


def _strict_json(payload: bytes, label: str) -> Mapping[str, Any]:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignCommissionAllocationError(f"{label} is invalid JSON") from exc
    return _mapping(raw, label)


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


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (_canonical_json(payload) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
