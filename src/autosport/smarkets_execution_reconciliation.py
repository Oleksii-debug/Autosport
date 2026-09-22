"""Fail-closed Smarkets execution readback reconciliation.

This module consumes provider evidence only. It never performs network access, handles
credentials, submits/cancels orders, mutates bankroll state, or establishes real-money
execution readiness. HTTP success is intentionally insufficient: acceptance truth is
derived only from exact provider readback bound to an existing canonical ExecutionAction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
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


def _decimal_input(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise SmarketsReconciliationError(
            f"{name} must be Decimal, decimal string, or non-boolean int"
        )
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SmarketsReconciliationError(f"{name} must be a finite Decimal") from exc
    return parsed


def _positive_decimal(value: object, name: str) -> Decimal:
    parsed = _decimal_input(value, name)
    if not parsed.is_finite() or parsed <= 0:
        raise SmarketsReconciliationError(f"{name} must be finite and > 0")
    return parsed


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    parsed = _decimal_input(value, name)
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
        if self.data_harvesting_approved:
            raise SmarketsReconciliationError(
                "Smarkets execution authority cannot approve data harvesting"
            )
        if self.benchmarking_approved:
            raise SmarketsReconciliationError(
                "Smarkets execution authority cannot approve benchmarking"
            )
        if self.redistribution_approved:
            raise SmarketsReconciliationError(
                "Smarkets execution authority cannot approve redistribution; "
                "use product-owned provider entitlement evidence"
            )

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
        if purpose is not SmarketsDataPurpose.EXECUTION:
            raise SmarketsReconciliationError(
                f"{purpose.value} is outside Smarkets execution-reconciliation authority"
            )
        if not self.execution_approved:
            raise SmarketsReconciliationError(
                "Smarkets authority does not approve execution"
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


_UINT64_MAX = (1 << 64) - 1
_PERCENT_PRICE_SCALE = 10_000
_QUANTITY_SCALE = 10_000
_STAKE_SCALE = Decimal(_PERCENT_PRICE_SCALE * _QUANTITY_SCALE)


def _provider_uint(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = _UINT64_MAX,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise SmarketsReconciliationError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _odds_range(start: str, stop: str, step: str) -> tuple[Decimal, ...]:
    current = Decimal(start)
    final = Decimal(stop)
    increment = Decimal(step)
    values: list[Decimal] = []
    while current <= final:
        values.append(current)
        current += increment
    return tuple(values)


# Smarkets' published exchange ladder. Provider prices are inverse probability
# percentages rounded to two decimals, represented by the API as integer units.
_SMK_ODDS_LADDER = frozenset(
    (Decimal("1.0001"),)
    + _odds_range("1.01", "2.00", "0.01")
    + _odds_range("2.02", "3.00", "0.02")
    + _odds_range("3.05", "4.00", "0.05")
    + _odds_range("4.1", "6.0", "0.1")
    + _odds_range("6.2", "10.0", "0.2")
    + _odds_range("10.5", "20.0", "0.5")
    + _odds_range("21", "30", "1")
    + _odds_range("32", "50", "2")
    + _odds_range("55", "100", "5")
    + _odds_range("110", "300", "10")
    + (Decimal("500"), Decimal("1000"), Decimal("10000"))
)


def _rounded_price_units(odds: Decimal) -> int:
    with localcontext() as context:
        context.prec = 50
        units = (Decimal(_PERCENT_PRICE_SCALE) / odds).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    return int(units)


_SMK_ODDS_TO_PRICE_UNITS = {
    odds: _rounded_price_units(odds) for odds in _SMK_ODDS_LADDER
}
_SMK_PRICE_UNITS_TO_ODDS = {
    units: odds for odds, units in _SMK_ODDS_TO_PRICE_UNITS.items()
}
if len(_SMK_PRICE_UNITS_TO_ODDS) != len(_SMK_ODDS_TO_PRICE_UNITS):
    raise RuntimeError("published Smarkets odds ladder has ambiguous percentage prices")


def _price_units_for_decimal_odds(odds: Decimal) -> int:
    try:
        return _SMK_ODDS_TO_PRICE_UNITS[odds]
    except KeyError as exc:
        raise SmarketsReconciliationError(
            "requested_odds is not on the published Smarkets exchange ladder"
        ) from exc


def _decimal_odds_from_price_units(price_units: int) -> Decimal:
    _provider_uint(
        price_units,
        "executed_avg_price_units",
        minimum=1,
        maximum=_PERCENT_PRICE_SCALE - 1,
    )
    published = _SMK_PRICE_UNITS_TO_ODDS.get(price_units)
    if published is not None:
        return published
    # Average execution prices can sit between exchange ticks. Preserve those
    # provider-native units in evidence and project them to Decimal at high precision.
    with localcontext() as context:
        context.prec = 50
        return +(Decimal(_PERCENT_PRICE_SCALE) / Decimal(price_units))


def _quantity_units_for_action(
    action: ExecutionAction, requested_price_units: int
) -> int:
    # Conceptually Smarkets quantity is payout/return (stake * odds). The
    # provider's canonical price is a percentage rounded to two decimals and
    # quantity itself is integer 1e-4 currency, so translating from a canonical
    # stake must also be bounded in provider units. Floor to the greatest
    # quantity whose requested-price stake cannot exceed the requested stake.
    _provider_uint(
        requested_price_units,
        "requested_price_units",
        minimum=1,
        maximum=_PERCENT_PRICE_SCALE - 1,
    )
    numerator = action.requested_stake * _STAKE_SCALE
    quantity_units = int(numerator // Decimal(requested_price_units))
    if quantity_units < 1:
        raise SmarketsReconciliationError(
            "requested stake is below one Smarkets quantity unit at this price"
        )
    return _provider_uint(
        quantity_units,
        "expected_requested_quantity_units",
        minimum=1,
    )


def _provider_side_for_action(action: ExecutionAction) -> str:
    if action.side == "BACK":
        return "buy"
    if action.side == "LAY":
        return "sell"
    raise SmarketsReconciliationError(
        "canonical action side must be BACK or LAY for Smarkets"
    )


@dataclass(frozen=True, slots=True)
class SmarketsOrderReadback:
    """Normalized official-API order readback in provider-native fixed-point units."""

    provider_order_id: str
    reference_id: str
    account_id: str
    event_id: str
    market_id: str
    contract_id: str
    side: str
    requested_price_units: int
    requested_quantity_units: int
    executed_quantity_units: int
    executed_avg_price_units: int | None
    state: SmarketsOrderState
    observed_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "provider_order_id",
            "reference_id",
            "account_id",
            "event_id",
            "market_id",
            "contract_id",
            "side",
        ):
            _text(getattr(self, name), name)
        if self.side not in {"buy", "sell"}:
            raise SmarketsReconciliationError("side must be Smarkets buy or sell")
        _provider_uint(
            self.requested_price_units,
            "requested_price_units",
            minimum=1,
            maximum=_PERCENT_PRICE_SCALE - 1,
        )
        _provider_uint(
            self.requested_quantity_units,
            "requested_quantity_units",
            minimum=1,
        )
        _provider_uint(
            self.executed_quantity_units,
            "executed_quantity_units",
        )
        if self.executed_quantity_units > self.requested_quantity_units:
            raise SmarketsReconciliationError(
                "executed_quantity_units exceeds requested_quantity_units"
            )
        if self.executed_avg_price_units is None:
            if self.executed_quantity_units != 0:
                raise SmarketsReconciliationError(
                    "executed order requires executed_avg_price_units"
                )
        else:
            _provider_uint(
                self.executed_avg_price_units,
                "executed_avg_price_units",
                minimum=1,
                maximum=_PERCENT_PRICE_SCALE - 1,
            )
            if self.executed_quantity_units == 0:
                raise SmarketsReconciliationError(
                    "unexecuted order must not claim executed_avg_price_units"
                )
        if type(self.state) is not SmarketsOrderState:
            raise SmarketsReconciliationError("state must be SmarketsOrderState")
        if self.state is SmarketsOrderState.FILLED and (
            self.executed_quantity_units != self.requested_quantity_units
        ):
            raise SmarketsReconciliationError(
                "FILLED must execute full requested_quantity_units"
            )
        if self.state is SmarketsOrderState.PARTIAL and not (
            0 < self.executed_quantity_units < self.requested_quantity_units
        ):
            raise SmarketsReconciliationError(
                "PARTIAL requires partial executed quantity"
            )
        if self.state is SmarketsOrderState.OPEN and self.executed_quantity_units != 0:
            raise SmarketsReconciliationError(
                "OPEN normalized state cannot claim executed quantity"
            )
        if self.state is SmarketsOrderState.REJECTED and self.executed_quantity_units != 0:
            raise SmarketsReconciliationError(
                "REJECTED cannot contain executed quantity"
            )
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.source_payload_sha256, "source_payload_sha256")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "contract_id": self.contract_id,
            "event_id": self.event_id,
            "executed_avg_price_units": self.executed_avg_price_units,
            "executed_quantity_units": self.executed_quantity_units,
            "market_id": self.market_id,
            "observed_at": self.observed_at,
            "provider_order_id": self.provider_order_id,
            "reference_id": self.reference_id,
            "requested_price_units": self.requested_price_units,
            "requested_quantity_units": self.requested_quantity_units,
            "side": self.side,
            "source_payload_sha256": self.source_payload_sha256,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class VerifiedSmarketsOrderEffect:
    action_id: str
    provider_order_id: str
    reference_id: str
    side: str
    requested_price_units: int
    requested_quantity_units: int
    status: AcknowledgementStatus
    accepted_odds: Decimal | None
    accepted_stake: Decimal
    accepted_liability: Decimal
    executed_quantity_units: int
    executed_avg_price_units: int | None
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
            "accepted_liability": _decimal_text(self.accepted_liability),
            "action_id": self.action_id,
            "authority_id": self.authority_id,
            "evidence_id": self.evidence_id,
            "executed_avg_price_units": self.executed_avg_price_units,
            "executed_quantity_units": self.executed_quantity_units,
            "observed_at": self.observed_at,
            "profile_id": self.profile_id,
            "provider_order_id": self.provider_order_id,
            "reference_id": self.reference_id,
            "requested_price_units": self.requested_price_units,
            "requested_quantity_units": self.requested_quantity_units,
            "side": self.side,
            "source_payload_sha256": self.source_payload_sha256,
            "status": self.status.value,
        }


def verify_smarkets_order_readback(
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    authority: SmarketsExecutionAuthority,
    readback: SmarketsOrderReadback,
    *,
    expected_reference_id: str,
) -> VerifiedSmarketsOrderEffect:
    """Promote exact provider readback into bounded canonical execution evidence.

    Smarkets' order quantity is payout/return (stake * odds) in 1e-4 units.
    Consequently a best-price fill can execute the full provider quantity while
    using less conventional stake. Provider fixed-point fields remain embedded
    in the evidence digest; HTTP success alone never creates this effect.
    """

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
    expected_reference = _text(expected_reference_id, "expected_reference_id")

    if profile.venue_id != action.bookmaker_id:
        raise SmarketsReconciliationError("bookmaker profile does not match action")
    if profile.venue_id.lower() != "smarkets":
        raise SmarketsReconciliationError("profile venue is not Smarkets")
    if profile.adapter_id != ADAPTER_ID or profile.adapter_version != ADAPTER_VERSION:
        raise SmarketsReconciliationError(
            "profile adapter identity is not this Smarkets official API adapter"
        )
    if profile.account_id != action.account_id or profile.account_id != readback.account_id:
        raise SmarketsReconciliationError("Smarkets account identity mismatch")
    for capability in (BookmakerCapability.PLACE_BET, BookmakerCapability.BET_READBACK):
        if profile.state_of(capability) is not BookmakerCapabilityState.SUPPORTED:
            raise SmarketsReconciliationError(
                f"Smarkets profile lacks supported {capability.value}"
            )

    action_time = _timestamp(action.quote_observed_at, "action.quote_observed_at")
    profile_time = _timestamp(profile.observed_at, "profile.observed_at")
    readback_time = _timestamp(readback.observed_at, "readback.observed_at")
    if profile_time > action_time:
        raise SmarketsReconciliationError(
            "Smarkets capability profile was not causally available at action time"
        )
    authority.assert_action_scope(action, as_of=action.quote_observed_at)
    if readback_time < action_time:
        raise SmarketsReconciliationError(
            "provider readback cannot predate the execution action evidence"
        )

    if readback.reference_id != expected_reference:
        raise SmarketsReconciliationError(
            "provider readback reference_id does not match durable submission identity"
        )
    if (
        readback.event_id != action.event_id
        or readback.market_id != action.market_id
        or readback.contract_id != action.selection_id
        or readback.side != _provider_side_for_action(action)
    ):
        raise SmarketsReconciliationError(
            "provider order identity conflicts with execution action"
        )

    expected_price_units = _price_units_for_decimal_odds(action.requested_odds)
    if readback.requested_price_units != expected_price_units:
        raise SmarketsReconciliationError(
            "provider requested price conflicts with canonical odds"
        )
    expected_quantity_units = _quantity_units_for_action(
        action, expected_price_units
    )
    if readback.requested_quantity_units != expected_quantity_units:
        raise SmarketsReconciliationError(
            "provider requested quantity conflicts with risk-bounded stake/price economics"
        )

    if readback.state is SmarketsOrderState.OPEN:
        raise SmarketsReconciliationPending(
            "open provider order does not prove terminal execution economics"
        )

    if readback.executed_quantity_units == 0:
        if readback.state not in (
            SmarketsOrderState.REJECTED,
            SmarketsOrderState.CANCELLED,
        ):
            raise SmarketsReconciliationPending(
                "provider readback does not prove a terminal zero-fill effect"
            )
        status = AcknowledgementStatus.REJECTED
        accepted_odds = None
        accepted_stake = Decimal("0")
        accepted_liability = Decimal("0")
    else:
        executed_price_units = readback.executed_avg_price_units
        if executed_price_units is None:
            raise SmarketsReconciliationError(
                "executed provider effect lacks executed_avg_price_units"
            )
        # Best-price execution must not worsen the requested exchange price.
        if readback.side == "buy" and executed_price_units > readback.requested_price_units:
            raise SmarketsReconciliationError(
                "Smarkets buy execution is worse than requested price"
            )
        if readback.side == "sell" and executed_price_units < readback.requested_price_units:
            raise SmarketsReconciliationError(
                "Smarkets sell execution is worse than requested price"
            )
        accepted_stake = (
            Decimal(readback.executed_quantity_units)
            * Decimal(executed_price_units)
            / _STAKE_SCALE
        )
        accepted_odds = _decimal_odds_from_price_units(executed_price_units)
        executed_payout = (
            Decimal(readback.executed_quantity_units) / Decimal(_QUANTITY_SCALE)
        )
        accepted_liability = (
            accepted_stake
            if action.side == "BACK"
            else executed_payout - accepted_stake
        )
        requested_liability = (
            action.requested_stake
            if action.side == "BACK"
            else action.requested_stake * (action.requested_odds - Decimal("1"))
        )
        if accepted_liability > requested_liability:
            raise SmarketsReconciliationError(
                "provider execution liability exceeds canonical requested liability"
            )
        status = (
            AcknowledgementStatus.ACCEPTED
            if readback.executed_quantity_units == readback.requested_quantity_units
            else AcknowledgementStatus.PARTIAL
        )
        if readback.state is SmarketsOrderState.REJECTED:
            raise SmarketsReconciliationError(
                "rejected provider state conflicts with executed quantity"
            )

    profile_id = profile.profile_id
    authority_id = authority.authority_id
    payload = {
        "schema": "autosport.smarkets_order_effect",
        "schema_version": 2,
        "action": action.to_dict(),
        "authority_id": authority_id,
        "profile_id": profile_id,
        "readback": readback.to_canonical_dict(),
        "resolved_status": status.value,
        "accepted_odds": (
            None if accepted_odds is None else _decimal_text(accepted_odds)
        ),
        "accepted_stake": _decimal_text(accepted_stake),
        "accepted_liability": _decimal_text(accepted_liability),
    }
    evidence_id = _digest(payload)
    return VerifiedSmarketsOrderEffect(
        action_id=action.action_id,
        provider_order_id=readback.provider_order_id,
        reference_id=readback.reference_id,
        side=readback.side,
        requested_price_units=readback.requested_price_units,
        requested_quantity_units=readback.requested_quantity_units,
        status=status,
        accepted_odds=accepted_odds,
        accepted_stake=accepted_stake,
        accepted_liability=accepted_liability,
        executed_quantity_units=readback.executed_quantity_units,
        executed_avg_price_units=readback.executed_avg_price_units,
        observed_at=readback.observed_at,
        authority_id=authority_id,
        profile_id=profile_id,
        source_payload_sha256=readback.source_payload_sha256,
        evidence_id=evidence_id,
    )


@dataclass(frozen=True, slots=True)
class SmarketsRateLimitEvidence:
    """Exact 429 evidence using Smarkets' relative X-RateLimit-Reset semantics."""

    observed_at: str
    reset_after_seconds: int
    source_payload_sha256: str
    http_status: int = 429
    error_type: str = "RATE_LIMIT_EXCEEDED"

    def __post_init__(self) -> None:
        _timestamp(self.observed_at, "observed_at")
        if type(self.reset_after_seconds) is not int or self.reset_after_seconds < 0:
            raise SmarketsReconciliationError(
                "reset_after_seconds must be a non-negative non-boolean int"
            )
        if type(self.http_status) is not int or self.http_status != 429:
            raise SmarketsReconciliationError(
                "Smarkets rate-limit evidence requires HTTP 429"
            )
        if self.error_type != "RATE_LIMIT_EXCEEDED":
            raise SmarketsReconciliationError(
                "Smarkets rate-limit evidence requires RATE_LIMIT_EXCEEDED"
            )
        _sha256(self.source_payload_sha256, "source_payload_sha256")

    @property
    def retry_not_before(self) -> datetime:
        return _timestamp(self.observed_at, "observed_at") + timedelta(
            seconds=self.reset_after_seconds
        )

    def retry_allowed(self, now: str) -> bool:
        return _timestamp(now, "now") >= self.retry_not_before


_EFFECT_RECORD_KEYS = {
    "accepted_odds",
    "accepted_stake",
    "accepted_liability",
    "action_id",
    "authority_id",
    "evidence_id",
    "executed_avg_price_units",
    "executed_quantity_units",
    "observed_at",
    "profile_id",
    "provider_order_id",
    "reference_id",
    "requested_price_units",
    "requested_quantity_units",
    "side",
    "source_payload_sha256",
    "status",
}


def _validated_effect_record(
    record: object,
) -> tuple[
    AcknowledgementStatus,
    Decimal,
    Decimal | None,
    Decimal,
    datetime,
    int,
    int | None,
]:
    if type(record) is not dict or set(record) != _EFFECT_RECORD_KEYS:
        raise SmarketsReconciliationError("invalid Smarkets effect record shape")
    _text(record["action_id"], "record.action_id")
    _text(record["provider_order_id"], "record.provider_order_id")
    _text(record["reference_id"], "record.reference_id")
    side = _text(record["side"], "record.side")
    if side not in {"buy", "sell"}:
        raise SmarketsReconciliationError("record.side must be buy or sell")
    requested_price_units = _provider_uint(
        record["requested_price_units"],
        "record.requested_price_units",
        minimum=1,
        maximum=_PERCENT_PRICE_SCALE - 1,
    )
    requested_quantity_units = _provider_uint(
        record["requested_quantity_units"],
        "record.requested_quantity_units",
        minimum=1,
    )
    executed_quantity_units = _provider_uint(
        record["executed_quantity_units"],
        "record.executed_quantity_units",
    )
    if executed_quantity_units > requested_quantity_units:
        raise SmarketsReconciliationError(
            "record executed quantity exceeds requested quantity"
        )
    executed_avg_price_units = record["executed_avg_price_units"]
    if executed_avg_price_units is not None:
        _provider_uint(
            executed_avg_price_units,
            "record.executed_avg_price_units",
            minimum=1,
            maximum=_PERCENT_PRICE_SCALE - 1,
        )
    for name in (
        "authority_id",
        "evidence_id",
        "profile_id",
        "source_payload_sha256",
    ):
        _sha256(record[name], f"record.{name}")
    observed = _timestamp(record["observed_at"], "record.observed_at")
    try:
        status = AcknowledgementStatus(record["status"])
    except (TypeError, ValueError) as exc:
        raise SmarketsReconciliationError(
            "record.status must be canonical acknowledgement status"
        ) from exc

    stake = _nonnegative_decimal(record["accepted_stake"], "record.accepted_stake")
    liability = _nonnegative_decimal(
        record["accepted_liability"], "record.accepted_liability"
    )
    odds_raw = record["accepted_odds"]
    odds = (
        None
        if odds_raw is None
        else _positive_decimal(odds_raw, "record.accepted_odds")
    )

    if executed_quantity_units == 0:
        if (
            status is not AcknowledgementStatus.REJECTED
            or stake != 0
            or liability != 0
            or odds is not None
            or executed_avg_price_units is not None
        ):
            raise SmarketsReconciliationError(
                "zero-fill record must be a zero-economics REJECTED effect"
            )
    else:
        if executed_avg_price_units is None:
            raise SmarketsReconciliationError(
                "executed effect record lacks average provider price"
            )
        expected_stake = (
            Decimal(executed_quantity_units)
            * Decimal(executed_avg_price_units)
            / _STAKE_SCALE
        )
        expected_odds = _decimal_odds_from_price_units(executed_avg_price_units)
        payout = Decimal(executed_quantity_units) / Decimal(_QUANTITY_SCALE)
        expected_liability = (
            expected_stake if side == "buy" else payout - expected_stake
        )
        expected_status = (
            AcknowledgementStatus.ACCEPTED
            if executed_quantity_units == requested_quantity_units
            else AcknowledgementStatus.PARTIAL
        )
        if status is not expected_status:
            raise SmarketsReconciliationError(
                "record status conflicts with executed provider quantity"
            )
        if stake != expected_stake or odds != expected_odds:
            raise SmarketsReconciliationError(
                "record accepted stake/odds conflicts with provider fixed-point economics"
            )
        if liability != expected_liability:
            raise SmarketsReconciliationError(
                "record accepted liability conflicts with provider fixed-point economics"
            )

    return (
        status,
        stake,
        odds,
        liability,
        observed,
        executed_quantity_units,
        executed_avg_price_units,
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
            _validated_effect_record(row["record"])
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
        effect_record = effect.to_canonical_dict()
        (
            new_status,
            new_stake,
            new_odds,
            new_liability,
            new_observed,
            new_quantity,
            new_avg_price,
        ) = _validated_effect_record(effect_record)
        for record in records:
            if record.get("evidence_id") == effect.evidence_id:
                if record == effect_record:
                    return
                raise SmarketsReconciliationError(
                    "evidence_id was reused with conflicting journal payload"
                )
            if record.get("reference_id") == effect.reference_id and (
                record.get("action_id") != effect.action_id
                or record.get("provider_order_id") != effect.provider_order_id
            ):
                raise SmarketsReconciliationError(
                    "reference_id conflicts with prior provider order/action"
                )
            if record.get("provider_order_id") != effect.provider_order_id:
                continue
            if record.get("action_id") != effect.action_id:
                raise SmarketsReconciliationError(
                    "provider_order_id conflicts with prior action"
                )
            if record.get("reference_id") != effect.reference_id:
                raise SmarketsReconciliationError(
                    "provider_order_id changed durable reference_id"
                )
            (
                old_status,
                old_stake,
                old_odds,
                old_liability,
                old_observed,
                old_quantity,
                old_avg_price,
            ) = _validated_effect_record(record)
            if new_observed < old_observed:
                raise SmarketsReconciliationError(
                    "provider order readback time regressed"
                )
            if new_quantity < old_quantity:
                raise SmarketsReconciliationError(
                    "provider order executed quantity regressed"
                )
            if new_quantity == old_quantity and (
                new_avg_price != old_avg_price
                or new_stake != old_stake
                or new_odds != old_odds
                or new_liability != old_liability
            ):
                raise SmarketsReconciliationError(
                    "provider economics changed without a new fill"
                )
            if old_status is AcknowledgementStatus.ACCEPTED and (
                new_status is not AcknowledgementStatus.ACCEPTED
                or new_quantity != old_quantity
                or new_avg_price != old_avg_price
            ):
                raise SmarketsReconciliationError(
                    "fully accepted provider order cannot regress"
                )
            if old_status is AcknowledgementStatus.REJECTED and (
                new_status is not AcknowledgementStatus.REJECTED
                or new_quantity != 0
            ):
                raise SmarketsReconciliationError(
                    "rejected provider order cannot later mint a fill"
                )
        previous = rows[-1]["record_sha256"] if rows else "0" * 64
        record = effect_record
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
