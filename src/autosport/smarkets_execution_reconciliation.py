"""Fail-closed Smarkets execution readback reconciliation.

This module consumes provider evidence only. It never performs network access, handles
credentials, submits/cancels orders, mutates bankroll state, or establishes real-money
execution readiness. HTTP success is intentionally insufficient: acceptance truth is
derived only from exact provider readback bound to an existing canonical ExecutionAction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from .real_execution_ledger import AcknowledgementStatus, ExecutionAction


ADAPTER_ID = "smarkets-official-api"
ADAPTER_VERSION = "1"


class SmarketsReconciliationError(RuntimeError):
    """Provider evidence is malformed, ambiguous, stale, or outside authority."""


class SmarketsReconciliationPending(SmarketsReconciliationError):
    """A durable order exists but current readback does not prove a terminal effect."""


class SmarketsOrderState(str, Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class SmarketsDataPurpose(str, Enum):
    EXECUTION = "execution"
    DATA_HARVESTING = "data_harvesting"
    REDISTRIBUTION = "redistribution"
    BENCHMARKING = "benchmarking"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise SmarketsReconciliationError(f"{name} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SmarketsReconciliationError(f"{name} must be valid UTF-8") from exc
    return value


def _timestamp(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SmarketsReconciliationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SmarketsReconciliationError(f"{name} must be timezone-aware")
    return parsed


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise SmarketsReconciliationError(f"{name} must be lowercase SHA-256 hex")
    return text


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SmarketsReconciliationError(f"{name} must be a finite Decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SmarketsReconciliationError(f"{name} must be finite and > 0")
    return parsed


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SmarketsReconciliationError(f"{name} must be a finite Decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise SmarketsReconciliationError(f"{name} must be finite and >= 0")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SmarketsReconciliationError("evidence is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class SmarketsExecutionAuthority:
    """External approval/account/scope evidence; never self-issued money authority."""

    account_id: str
    approval_id: str
    approved_event_ids: tuple[str, ...]
    approved_market_ids: tuple[str, ...]
    observed_at: str
    expires_at: str
    source_ref: str
    source_payload_sha256: str
    execution_approved: bool
    data_harvesting_approved: bool = False
    redistribution_approved: bool = False
    benchmarking_approved: bool = False

    def __post_init__(self) -> None:
        _text(self.account_id, "account_id")
        _text(self.approval_id, "approval_id")
        if type(self.approved_event_ids) is not tuple or not self.approved_event_ids:
            raise SmarketsReconciliationError("approved_event_ids must be a non-empty tuple")
        if type(self.approved_market_ids) is not tuple or not self.approved_market_ids:
            raise SmarketsReconciliationError("approved_market_ids must be a non-empty tuple")
        for name, values in (
            ("approved_event_ids", self.approved_event_ids),
            ("approved_market_ids", self.approved_market_ids),
        ):
            for value in values:
                _text(value, name)
            if len(set(values)) != len(values):
                raise SmarketsReconciliationError(f"{name} must not contain duplicates")
        observed = _timestamp(self.observed_at, "observed_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= observed:
            raise SmarketsReconciliationError("expires_at must be after observed_at")
        _text(self.source_ref, "source_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")
        for name in (
            "execution_approved",
            "data_harvesting_approved",
            "redistribution_approved",
            "benchmarking_approved",
        ):
            if type(getattr(self, name)) is not bool:
                raise SmarketsReconciliationError(f"{name} must be bool")

    @property
    def authority_id(self) -> str:
        return _digest(
            {
                "account_id": self.account_id,
                "approval_id": self.approval_id,
                "approved_event_ids": sorted(self.approved_event_ids),
                "approved_market_ids": sorted(self.approved_market_ids),
                "benchmarking_approved": self.benchmarking_approved,
                "data_harvesting_approved": self.data_harvesting_approved,
                "execution_approved": self.execution_approved,
                "expires_at": self.expires_at,
                "observed_at": self.observed_at,
                "redistribution_approved": self.redistribution_approved,
                "source_payload_sha256": self.source_payload_sha256,
                "source_ref": self.source_ref,
            }
        )

    def require_purpose(self, purpose: SmarketsDataPurpose) -> None:
        if type(purpose) is not SmarketsDataPurpose:
            raise SmarketsReconciliationError("purpose must be SmarketsDataPurpose")
        allowed = {
            SmarketsDataPurpose.EXECUTION: self.execution_approved,
            SmarketsDataPurpose.DATA_HARVESTING: self.data_harvesting_approved,
            SmarketsDataPurpose.REDISTRIBUTION: self.redistribution_approved,
            SmarketsDataPurpose.BENCHMARKING: self.benchmarking_approved,
        }[purpose]
        if not allowed:
            raise SmarketsReconciliationError(
                f"Smarkets authority does not approve {purpose.value}"
            )

    def assert_action_scope(self, action: ExecutionAction, *, as_of: str) -> None:
        if type(action) is not ExecutionAction:
            raise SmarketsReconciliationError("action must be canonical ExecutionAction")
        self.require_purpose(SmarketsDataPurpose.EXECUTION)
        now = _timestamp(as_of, "as_of")
        if now < _timestamp(self.observed_at, "observed_at"):
            raise SmarketsReconciliationError("authority is not yet causally available")
        if now >= _timestamp(self.expires_at, "expires_at"):
            raise SmarketsReconciliationError("Smarkets authority evidence has expired")
        if action.account_id != self.account_id:
            raise SmarketsReconciliationError("action account is outside Smarkets authority")
        if action.event_id not in self.approved_event_ids:
            raise SmarketsReconciliationError("action event is outside approved Smarkets scope")
        if action.market_id not in self.approved_market_ids:
            raise SmarketsReconciliationError("action market is outside approved Smarkets scope")


@dataclass(frozen=True, slots=True)
class SmarketsOrderReadback:
    provider_order_id: str
    account_id: str
    event_id: str
    market_id: str
    contract_id: str
    side: str
    requested_price: Decimal | str | int
    requested_quantity: Decimal | str | int
    matched_quantity: Decimal | str | int
    average_matched_price: Decimal | str | int | None
    state: SmarketsOrderState
    observed_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "provider_order_id",
            "account_id",
            "event_id",
            "market_id",
            "contract_id",
            "side",
        ):
            _text(getattr(self, name), name)
        object.__setattr__(
            self, "requested_price", _positive_decimal(self.requested_price, "requested_price")
        )
        object.__setattr__(
            self,
            "requested_quantity",
            _positive_decimal(self.requested_quantity, "requested_quantity"),
        )
        object.__setattr__(
            self,
            "matched_quantity",
            _nonnegative_decimal(self.matched_quantity, "matched_quantity"),
        )
        if self.matched_quantity > self.requested_quantity:
            raise SmarketsReconciliationError("matched_quantity exceeds requested_quantity")
        if self.average_matched_price is not None:
            object.__setattr__(
                self,
                "average_matched_price",
                _positive_decimal(self.average_matched_price, "average_matched_price"),
            )
        if self.matched_quantity > 0 and self.average_matched_price is None:
            raise SmarketsReconciliationError(
                "matched order requires average_matched_price"
            )
        if self.matched_quantity == 0 and self.average_matched_price is not None:
            raise SmarketsReconciliationError(
                "unmatched order must not claim average_matched_price"
            )
        if type(self.state) is not SmarketsOrderState:
            raise SmarketsReconciliationError("state must be SmarketsOrderState")
        if self.state is SmarketsOrderState.FILLED and (
            self.matched_quantity != self.requested_quantity
        ):
            raise SmarketsReconciliationError("FILLED must match full requested_quantity")
        if self.state is SmarketsOrderState.PARTIAL and not (
            Decimal("0") < self.matched_quantity < self.requested_quantity
        ):
            raise SmarketsReconciliationError("PARTIAL requires partial matched quantity")
        if self.state is SmarketsOrderState.REJECTED and self.matched_quantity != 0:
            raise SmarketsReconciliationError("REJECTED cannot contain matched quantity")
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.source_payload_sha256, "source_payload_sha256")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "average_matched_price": (
                None
                if self.average_matched_price is None
                else _decimal_text(self.average_matched_price)
            ),
            "contract_id": self.contract_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "matched_quantity": _decimal_text(self.matched_quantity),
            "observed_at": self.observed_at,
            "provider_order_id": self.provider_order_id,
            "requested_price": _decimal_text(self.requested_price),
            "requested_quantity": _decimal_text(self.requested_quantity),
            "side": self.side,
            "source_payload_sha256": self.source_payload_sha256,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class VerifiedSmarketsOrderEffect:
    action_id: str
    provider_order_id: str
    status: AcknowledgementStatus
    accepted_odds: Decimal | None
    accepted_stake: Decimal
    observed_at: str
    authority_id: str
    profile_id: str
    source_payload_sha256: str
    evidence_id: str

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "accepted_odds": (
                None if self.accepted_odds is None else _decimal_text(self.accepted_odds)
            ),
            "accepted_stake": _decimal_text(self.accepted_stake),
            "action_id": self.action_id,
            "authority_id": self.authority_id,
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at,
            "profile_id": self.profile_id,
            "provider_order_id": self.provider_order_id,
            "source_payload_sha256": self.source_payload_sha256,
            "status": self.status.value,
        }


def verify_smarkets_order_readback(
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    authority: SmarketsExecutionAuthority,
    readback: SmarketsOrderReadback,
) -> VerifiedSmarketsOrderEffect:
    """Promote provider readback to bounded canonical acknowledgement evidence."""

    if type(action) is not ExecutionAction:
        raise SmarketsReconciliationError("action must be canonical ExecutionAction")
    if type(profile) is not BookmakerCapabilityProfile:
        raise SmarketsReconciliationError(
            "profile must be canonical BookmakerCapabilityProfile"
        )
    if type(authority) is not SmarketsExecutionAuthority:
        raise SmarketsReconciliationError("authority must be SmarketsExecutionAuthority")
    if type(readback) is not SmarketsOrderReadback:
        raise SmarketsReconciliationError("readback must be SmarketsOrderReadback")
    if profile.venue_id != action.bookmaker_id:
        raise SmarketsReconciliationError("bookmaker profile does not match action")
    if profile.venue_id.lower() != "smarkets":
        raise SmarketsReconciliationError("profile venue is not Smarkets")
    if profile.account_id != action.account_id or profile.account_id != readback.account_id:
        raise SmarketsReconciliationError("Smarkets account identity mismatch")
    for capability in (BookmakerCapability.PLACE_BET, BookmakerCapability.BET_READBACK):
        if profile.state_of(capability) is not BookmakerCapabilityState.SUPPORTED:
            raise SmarketsReconciliationError(
                f"Smarkets profile lacks supported {capability.value}"
            )

    authority.assert_action_scope(action, as_of=readback.observed_at)
    if (
        readback.event_id != action.event_id
        or readback.market_id != action.market_id
        or readback.contract_id != action.selection_id
        or readback.side != action.side
    ):
        raise SmarketsReconciliationError(
            "provider order identity conflicts with execution action"
        )
    if readback.requested_price != action.requested_odds:
        raise SmarketsReconciliationError(
            "provider requested price conflicts with execution action"
        )
    if readback.requested_quantity != action.requested_stake:
        raise SmarketsReconciliationError(
            "provider requested quantity conflicts with execution action"
        )

    if readback.state is SmarketsOrderState.OPEN and readback.matched_quantity == 0:
        raise SmarketsReconciliationPending(
            "open unmatched provider order does not prove acceptance economics"
        )

    if readback.matched_quantity == 0:
        if readback.state not in (
            SmarketsOrderState.REJECTED,
            SmarketsOrderState.CANCELLED,
        ):
            raise SmarketsReconciliationPending(
                "provider readback does not prove a terminal zero-fill effect"
            )
        status = AcknowledgementStatus.REJECTED
        accepted_odds = None
    else:
        accepted_odds = readback.average_matched_price
        if accepted_odds is None:
            raise SmarketsReconciliationError("matched effect lacks accepted odds")
        status = (
            AcknowledgementStatus.ACCEPTED
            if readback.matched_quantity == action.requested_stake
            else AcknowledgementStatus.PARTIAL
        )
        if readback.state is SmarketsOrderState.REJECTED:
            raise SmarketsReconciliationError(
                "rejected provider state conflicts with matched quantity"
            )

    profile_id = profile.profile_id
    authority_id = authority.authority_id
    payload = {
        "schema": "autosport.smarkets_order_effect",
        "schema_version": 1,
        "action": action.to_dict(),
        "authority_id": authority_id,
        "profile_id": profile_id,
        "readback": readback.to_canonical_dict(),
        "resolved_status": status.value,
        "accepted_odds": (
            None if accepted_odds is None else _decimal_text(accepted_odds)
        ),
        "accepted_stake": _decimal_text(readback.matched_quantity),
    }
    evidence_id = _digest(payload)
    return VerifiedSmarketsOrderEffect(
        action_id=action.action_id,
        provider_order_id=readback.provider_order_id,
        status=status,
        accepted_odds=accepted_odds,
        accepted_stake=readback.matched_quantity,
        observed_at=readback.observed_at,
        authority_id=authority_id,
        profile_id=profile_id,
        source_payload_sha256=readback.source_payload_sha256,
        evidence_id=evidence_id,
    )


@dataclass(frozen=True, slots=True)
class SmarketsRateLimitEvidence:
    observed_at: str
    provider_reset_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        observed = _timestamp(self.observed_at, "observed_at")
        reset = _timestamp(self.provider_reset_at, "provider_reset_at")
        if reset <= observed:
            raise SmarketsReconciliationError(
                "provider_reset_at must be after observed_at"
            )
        _sha256(self.source_payload_sha256, "source_payload_sha256")

    def retry_allowed(self, now: str) -> bool:
        return _timestamp(now, "now") >= _timestamp(
            self.provider_reset_at, "provider_reset_at"
        )


class SmarketsReconciliationJournal:
    """Small append-only hash-chain for restart-safe verified readback evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SmarketsReconciliationError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def _load_rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise SmarketsReconciliationError("cannot read Smarkets journal") from exc
        rows: list[dict[str, Any]] = []
        previous = "0" * 64
        for index, raw in enumerate(raw_lines, start=1):
            if not raw:
                raise SmarketsReconciliationError("blank Smarkets journal record")
            try:
                row = json.loads(
                    raw,
                    object_pairs_hook=self._pairs,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        SmarketsReconciliationError(
                            f"non-finite JSON constant {value!r}"
                        )
                    ),
                )
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise SmarketsReconciliationError(
                    f"malformed Smarkets journal record {index}"
                ) from exc
            if type(row) is not dict or set(row) != {
                "prev_sha256",
                "record",
                "record_sha256",
                "schema_version",
            }:
                raise SmarketsReconciliationError(
                    f"invalid Smarkets journal shape at record {index}"
                )
            if row["schema_version"] != 1 or row["prev_sha256"] != previous:
                raise SmarketsReconciliationError(
                    f"Smarkets journal chain mismatch at record {index}"
                )
            _sha256(row["record_sha256"], "record_sha256")
            expected = _digest(
                {
                    "prev_sha256": row["prev_sha256"],
                    "record": row["record"],
                    "schema_version": 1,
                }
            )
            if row["record_sha256"] != expected:
                raise SmarketsReconciliationError(
                    f"Smarkets journal digest mismatch at record {index}"
                )
            previous = row["record_sha256"]
            rows.append(row)
        return rows

    def verify(self) -> tuple[dict[str, Any], ...]:
        return tuple(row["record"] for row in self._load_rows())

    def append(self, effect: VerifiedSmarketsOrderEffect) -> None:
        if type(effect) is not VerifiedSmarketsOrderEffect:
            raise SmarketsReconciliationError(
                "journal accepts only VerifiedSmarketsOrderEffect"
            )
        rows = self._load_rows()
        records = [row["record"] for row in rows]
        for record in records:
            if record.get("evidence_id") == effect.evidence_id:
                if record == effect.to_canonical_dict():
                    return
                raise SmarketsReconciliationError(
                    "evidence_id was reused with conflicting journal payload"
                )
            if (
                record.get("provider_order_id") == effect.provider_order_id
                and record.get("action_id") != effect.action_id
            ):
                raise SmarketsReconciliationError(
                    "provider_order_id conflicts with prior action"
                )
        previous = rows[-1]["record_sha256"] if rows else "0" * 64
        record = effect.to_canonical_dict()
        row = {
            "prev_sha256": previous,
            "record": record,
            "schema_version": 1,
        }
        row["record_sha256"] = _digest(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(_canonical(row).decode("ascii") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise SmarketsReconciliationError("cannot append Smarkets journal") from exc
