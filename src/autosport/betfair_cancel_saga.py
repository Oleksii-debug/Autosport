"""Crash-safe truth contract for Betfair ''cancelOrders'' composition.

This module deliberately owns no provider transport and grants no execution
authority.  It persists an exact cancellation intent before a caller may cross
an external boundary, records ambiguity without making retry safe, and adopts
provider/readback evidence without erasing already-matched exposure.

The provider request projection is intentionally narrow:
* SINGLE_ORDER always names marketId + betId;
* MARKET_UNMATCHED deliberately names marketId and omits instructions;
* ACCOUNT_ALL_LIMIT deliberately omits marketId and instructions and therefore
  requires an explicit account-emergency authority class.

A caller must still perform its own current owner/risk/provider-write checks
immediately before I/O.  This journal is recovery/evidence truth only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from .integrity import durable_path_lock

SCHEMA_VERSION = 1
CANCEL_ORDERS_METHOD = "SportsAPING/v1.0/cancelOrders"
_PROVIDER_ID = "betfair"
_ZERO_SHA256 = "0" * 64


class BetfairCancelSagaError(RuntimeError):
    """Base error for durable cancel-saga operations."""


class BetfairCancelSagaIntegrityError(BetfairCancelSagaError):
    """Durable cancellation evidence is malformed, torn, or tampered."""


class BetfairCancelSagaStateError(BetfairCancelSagaError):
    """A cancellation transition is unsafe from current durable state."""


class BetfairCancelSagaIdentityConflict(BetfairCancelSagaError):
    """A stable identity was reused with different immutable semantics."""


class CancelScope(StrEnum):
    SINGLE_ORDER = "SINGLE_ORDER"
    MARKET_UNMATCHED = "MARKET_UNMATCHED"
    ACCOUNT_ALL_LIMIT = "ACCOUNT_ALL_LIMIT"


class CancelAuthorityScope(StrEnum):
    SINGLE_ORDER = "SINGLE_ORDER"
    MARKET_UNMATCHED = "MARKET_UNMATCHED"
    ACCOUNT_EMERGENCY = "ACCOUNT_EMERGENCY"


class CancelOrderType(StrEnum):
    LIMIT = "LIMIT"
    LIMIT_ON_CLOSE = "LIMIT_ON_CLOSE"
    MARKET_ON_CLOSE = "MARKET_ON_CLOSE"


class CancelProviderStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PROCESSED_WITH_ERRORS = "PROCESSED_WITH_ERRORS"
    UNKNOWN = "UNKNOWN"


class CancelSagaState(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTED_UNKNOWN = "SUBMITTED_UNKNOWN"
    PROVIDER_RESULT_UNVERIFIED = "PROVIDER_RESULT_UNVERIFIED"
    CANCEL_RECONCILED = "CANCEL_RECONCILED"
    NO_EFFECT_RECONCILED = "NO_EFFECT_RECONCILED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class CancelRetryDisposition(StrEnum):
    EXPLICIT_EXECUTION_POLICY_REQUIRED = "EXPLICIT_EXECUTION_POLICY_REQUIRED"
    READBACK_REQUIRED = "READBACK_REQUIRED"
    TERMINAL_NO_RETRY = "TERMINAL_NO_RETRY"
    CONFLICT_REQUIRES_OPERATOR = "CONFLICT_REQUIRES_OPERATOR"


class EventType(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    PROVIDER_RESULT = "PROVIDER_RESULT"
    RECONCILIATION = "RECONCILIATION"


def _text(value: object, name: str, *, max_length: int | None = None) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} exceeds maximum length {max_length}")
    return value


def _sha256(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return raw


def _timestamp(value: object, name: str) -> str:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    if type(value) is Decimal:
        parsed = value
    elif type(value) in {str, int}:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be an exact finite Decimal") from exc
    else:
        raise ValueError(f"{name} must be an exact Decimal-compatible value")
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be finite and >= 0")
    return parsed


def _positive_decimal(value: object, name: str) -> Decimal:
    parsed = _nonnegative_decimal(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be > 0")
    return parsed


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairCancelSagaIntegrityError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BetfairCancelSagaIntegrityError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> None:
    raise BetfairCancelSagaIntegrityError(f"non-finite JSON number {token!r}")


def _load_json_line(raw: str) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise BetfairCancelSagaIntegrityError("cancel journal contains invalid JSON") from exc
    if type(value) is not dict:
        raise BetfairCancelSagaIntegrityError("cancel journal event must be an object")
    return value


@dataclass(frozen=True, slots=True)
class CancelOrderEvidence:
    """Exact current LIMIT-order evidence used to bound a single-order cancel."""

    account_id: str
    market_id: str
    selection_id: str
    side: str
    bet_id: str
    order_type: CancelOrderType
    size_matched: Decimal | str | int
    size_remaining: Decimal | str | int
    size_cancelled: Decimal | str | int
    observed_at: str
    evidence_id: str

    def __post_init__(self) -> None:
        for name in ("account_id", "market_id", "selection_id", "bet_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        side = _text(self.side, "side")
        if side not in {"BACK", "LAY"}:
            raise ValueError("side must be BACK or LAY")
        object.__setattr__(self, "side", side)
        if type(self.order_type) is not CancelOrderType:
            raise ValueError("order_type must be exact CancelOrderType")
        if self.order_type is not CancelOrderType.LIMIT:
            raise ValueError("ordinary cancelOrders source evidence must be LIMIT")
        matched = _nonnegative_decimal(self.size_matched, "size_matched")
        remaining = _nonnegative_decimal(self.size_remaining, "size_remaining")
        cancelled = _nonnegative_decimal(self.size_cancelled, "size_cancelled")
        if remaining <= 0:
            raise ValueError("single-order cancellation requires executable remainder")
        object.__setattr__(self, "size_matched", matched)
        object.__setattr__(self, "size_remaining", remaining)
        object.__setattr__(self, "size_cancelled", cancelled)
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "evidence_id", _sha256(self.evidence_id, "evidence_id"))

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "bet_id": self.bet_id,
            "order_type": self.order_type.value,
            "size_matched": _decimal_text(self.size_matched),
            "size_remaining": _decimal_text(self.size_remaining),
            "size_cancelled": _decimal_text(self.size_cancelled),
            "observed_at": self.observed_at,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True, slots=True)
class CancelIntent:
    """Immutable cancellation intent persisted before any provider boundary."""

    saga_id: str
    account_id: str
    environment: str
    scope: CancelScope
    authority_scope: CancelAuthorityScope
    authority_ref: str
    authority_sha256: str
    prepared_at: str
    market_id: str | None = None
    bet_id: str | None = None
    cancel_size: Decimal | str | int | None = None
    source_order: CancelOrderEvidence | None = None

    def __post_init__(self) -> None:
        for name in ("saga_id", "account_id", "environment", "authority_ref"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if type(self.scope) is not CancelScope:
            raise ValueError("scope must be exact CancelScope")
        if type(self.authority_scope) is not CancelAuthorityScope:
            raise ValueError("authority_scope must be exact CancelAuthorityScope")
        object.__setattr__(
            self,
            "authority_sha256",
            _sha256(self.authority_sha256, "authority_sha256"),
        )
        object.__setattr__(self, "prepared_at", _timestamp(self.prepared_at, "prepared_at"))

        expected_authority = {
            CancelScope.SINGLE_ORDER: CancelAuthorityScope.SINGLE_ORDER,
            CancelScope.MARKET_UNMATCHED: CancelAuthorityScope.MARKET_UNMATCHED,
            CancelScope.ACCOUNT_ALL_LIMIT: CancelAuthorityScope.ACCOUNT_EMERGENCY,
        }[self.scope]
        if self.authority_scope is not expected_authority:
            raise ValueError("cancel scope does not match explicit authority scope")

        if self.scope is CancelScope.SINGLE_ORDER:
            market_id = _text(self.market_id, "market_id")
            bet_id = _text(self.bet_id, "bet_id")
            if type(self.source_order) is not CancelOrderEvidence:
                raise ValueError("single-order cancellation requires exact source_order evidence")
            if (
                self.source_order.account_id != self.account_id
                or self.source_order.market_id != market_id
                or self.source_order.bet_id != bet_id
            ):
                raise ValueError("single-order intent mismatches source order identity")
            if _time(self.source_order.observed_at) > _time(self.prepared_at):
                raise ValueError("source order evidence cannot postdate prepared_at")
            size = (
                None
                if self.cancel_size is None
                else _positive_decimal(self.cancel_size, "cancel_size")
            )
            if size is not None and size > self.source_order.size_remaining:
                raise ValueError("cancel_size exceeds current unmatched remainder")
            object.__setattr__(self, "market_id", market_id)
            object.__setattr__(self, "bet_id", bet_id)
            object.__setattr__(self, "cancel_size", size)
        elif self.scope is CancelScope.MARKET_UNMATCHED:
            object.__setattr__(self, "market_id", _text(self.market_id, "market_id"))
            if self.bet_id is not None or self.cancel_size is not None or self.source_order is not None:
                raise ValueError("market-wide cancel cannot carry single-order fields")
        else:
            if (
                self.market_id is not None
                or self.bet_id is not None
                or self.cancel_size is not None
                or self.source_order is not None
            ):
                raise ValueError("account-wide cancel must not carry narrowing fields")

    def _core_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.betfair_cancel_intent",
            "schema_version": SCHEMA_VERSION,
            "provider_id": _PROVIDER_ID,
            "saga_id": self.saga_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "scope": self.scope.value,
            "authority_scope": self.authority_scope.value,
            "authority_ref": self.authority_ref,
            "authority_sha256": self.authority_sha256,
            "prepared_at": self.prepared_at,
            "market_id": self.market_id,
            "bet_id": self.bet_id,
            "cancel_size": None if self.cancel_size is None else _decimal_text(self.cancel_size),
            "source_order": None if self.source_order is None else self.source_order.to_dict(),
        }

    @property
    def semantic_sha256(self) -> str:
        return _digest(self._core_payload())

    @property
    def customer_ref(self) -> str:
        """Provider dedupe aid only; never durable exactly-once truth."""

        return hashlib.sha256(
            f"autosport:cancel:{self.semantic_sha256}".encode("utf-8")
        ).hexdigest()[:32]

    @property
    def serialization_key(self) -> str:
        """Stable external serialization key; this module does not acquire it."""

        if self.scope is CancelScope.ACCOUNT_ALL_LIMIT:
            return f"{_PROVIDER_ID}:{self.account_id}:ACCOUNT_ALL_LIMIT"
        if self.scope is CancelScope.MARKET_UNMATCHED:
            return f"{_PROVIDER_ID}:{self.account_id}:market:{self.market_id}"
        return f"{_PROVIDER_ID}:{self.account_id}:bet:{self.bet_id}"

    def provider_request(self) -> dict[str, object]:
        params: dict[str, object] = {"customerRef": self.customer_ref}
        if self.scope is CancelScope.SINGLE_ORDER:
            instruction: dict[str, object] = {"betId": self.bet_id}
            if self.cancel_size is not None:
                instruction["sizeReduction"] = _decimal_text(self.cancel_size)
            params["marketId"] = self.market_id
            params["instructions"] = [instruction]
        elif self.scope is CancelScope.MARKET_UNMATCHED:
            params["marketId"] = self.market_id
        return {
            "jsonrpc": "2.0",
            "method": CANCEL_ORDERS_METHOD,
            "params": params,
        }

    @property
    def request_sha256(self) -> str:
        return _digest(self.provider_request())

    def to_dict(self) -> dict[str, object]:
        return {
            **self._core_payload(),
            "semantic_sha256": self.semantic_sha256,
            "customer_ref": self.customer_ref,
            "serialization_key": self.serialization_key,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True, slots=True)
class CancelInstructionEffect:
    bet_id: str
    size_cancelled: Decimal | str | int
    size_matched: Decimal | str | int
    size_remaining: Decimal | str | int

    def __post_init__(self) -> None:
        object.__setattr__(self, "bet_id", _text(self.bet_id, "bet_id"))
        for name in ("size_cancelled", "size_matched", "size_remaining"):
            object.__setattr__(
                self,
                name,
                _nonnegative_decimal(getattr(self, name), name),
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "bet_id": self.bet_id,
            "size_cancelled": _decimal_text(self.size_cancelled),
            "size_matched": _decimal_text(self.size_matched),
            "size_remaining": _decimal_text(self.size_remaining),
        }


@dataclass(frozen=True, slots=True)
class CancelProviderEvidence:
    evidence_id: str
    response_sha256: str
    observed_at: str
    source: str
    status: CancelProviderStatus
    error_code: str | None = None
    effects: tuple[CancelInstructionEffect, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _sha256(self.evidence_id, "evidence_id"))
        object.__setattr__(
            self,
            "response_sha256",
            _sha256(self.response_sha256, "response_sha256"),
        )
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        if type(self.status) is not CancelProviderStatus:
            raise ValueError("status must be exact CancelProviderStatus")
        if self.error_code is not None:
            object.__setattr__(self, "error_code", _text(self.error_code, "error_code"))
        effects = tuple(self.effects)
        if not all(type(item) is CancelInstructionEffect for item in effects):
            raise ValueError("effects must contain exact CancelInstructionEffect values")
        if len({item.bet_id for item in effects}) != len(effects):
            raise ValueError("provider evidence requires unique bet_id effects")
        object.__setattr__(self, "effects", effects)
        if self.status is CancelProviderStatus.FAILURE and self.error_code is None:
            raise ValueError("provider FAILURE requires error_code")
        if self.status is CancelProviderStatus.SUCCESS and self.error_code is not None:
            raise ValueError("provider SUCCESS cannot carry top-level error_code")

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "response_sha256": self.response_sha256,
            "observed_at": self.observed_at,
            "source": self.source,
            "status": self.status.value,
            "error_code": self.error_code,
            "effects": [item.to_dict() for item in self.effects],
        }


@dataclass(frozen=True, slots=True)
class CancelOrderReadback:
    account_id: str
    market_id: str
    selection_id: str
    side: str
    bet_id: str
    order_type: CancelOrderType
    size_matched: Decimal | str | int
    size_remaining: Decimal | str | int
    size_cancelled: Decimal | str | int
    size_lapsed: Decimal | str | int = Decimal("0")
    size_voided: Decimal | str | int = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("account_id", "market_id", "selection_id", "bet_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        side = _text(self.side, "side")
        if side not in {"BACK", "LAY"}:
            raise ValueError("side must be BACK or LAY")
        object.__setattr__(self, "side", side)
        if type(self.order_type) is not CancelOrderType:
            raise ValueError("order_type must be exact CancelOrderType")
        for name in (
            "size_matched",
            "size_remaining",
            "size_cancelled",
            "size_lapsed",
            "size_voided",
        ):
            object.__setattr__(
                self,
                name,
                _nonnegative_decimal(getattr(self, name), name),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "bet_id": self.bet_id,
            "order_type": self.order_type.value,
            "size_matched": _decimal_text(self.size_matched),
            "size_remaining": _decimal_text(self.size_remaining),
            "size_cancelled": _decimal_text(self.size_cancelled),
            "size_lapsed": _decimal_text(self.size_lapsed),
            "size_voided": _decimal_text(self.size_voided),
        }


@dataclass(frozen=True, slots=True)
class CancelReconciliationEvidence:
    evidence_id: str
    observed_at: str
    source: str
    scope_complete: bool
    orders: tuple[CancelOrderReadback, ...] = ()
    absent_bet_ids: tuple[str, ...] = ()
    final_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _sha256(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        if type(self.scope_complete) is not bool:
            raise ValueError("scope_complete must be bool")
        orders = tuple(self.orders)
        if not all(type(item) is CancelOrderReadback for item in orders):
            raise ValueError("orders must contain exact CancelOrderReadback values")
        if len({item.bet_id for item in orders}) != len(orders):
            raise ValueError("reconciliation requires unique order bet_ids")
        object.__setattr__(self, "orders", orders)
        absent = tuple(_text(value, "absent_bet_id") for value in self.absent_bet_ids)
        if len(set(absent)) != len(absent):
            raise ValueError("absent_bet_ids must be unique")
        if set(absent) & {item.bet_id for item in orders}:
            raise ValueError("one bet_id cannot be both present and absent")
        object.__setattr__(self, "absent_bet_ids", absent)
        if self.final_evidence_sha256 is not None:
            object.__setattr__(
                self,
                "final_evidence_sha256",
                _sha256(self.final_evidence_sha256, "final_evidence_sha256"),
            )
        if absent and self.final_evidence_sha256 is None:
            raise ValueError("order absence requires explicit final/cleared evidence")

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at,
            "source": self.source,
            "scope_complete": self.scope_complete,
            "orders": [item.to_dict() for item in self.orders],
            "absent_bet_ids": list(self.absent_bet_ids),
            "final_evidence_sha256": self.final_evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class CancelExposureView:
    matched_bet_ids: tuple[str, ...]
    executable_bet_ids: tuple[str, ...]
    cancelled_remainder_bet_ids: tuple[str, ...]
    unresolved_bet_ids: tuple[str, ...]
    provider_verified: bool
    portfolio_reset_proven: bool = False
    execution_admission_eligible: bool = False
    real_money_authorized: bool = False


@dataclass(frozen=True, slots=True)
class CancelSagaSnapshot:
    intent: CancelIntent
    state: CancelSagaState
    retry_disposition: CancelRetryDisposition
    exposure: CancelExposureView
    event_count: int
    journal_sha256: str


def _source_order_from_dict(value: object) -> CancelOrderEvidence:
    if type(value) is not dict:
        raise BetfairCancelSagaIntegrityError("source_order must be an object")
    expected = {
        "account_id", "market_id", "selection_id", "side", "bet_id", "order_type",
        "size_matched", "size_remaining", "size_cancelled", "observed_at", "evidence_id",
    }
    if set(value) != expected:
        raise BetfairCancelSagaIntegrityError("source_order schema invalid")
    try:
        return CancelOrderEvidence(
            account_id=value["account_id"],
            market_id=value["market_id"],
            selection_id=value["selection_id"],
            side=value["side"],
            bet_id=value["bet_id"],
            order_type=CancelOrderType(value["order_type"]),
            size_matched=value["size_matched"],
            size_remaining=value["size_remaining"],
            size_cancelled=value["size_cancelled"],
            observed_at=value["observed_at"],
            evidence_id=value["evidence_id"],
        )
    except (TypeError, ValueError) as exc:
        raise BetfairCancelSagaIntegrityError("source_order payload invalid") from exc


def _intent_from_dict(value: object) -> CancelIntent:
    if type(value) is not dict:
        raise BetfairCancelSagaIntegrityError("cancel intent must be an object")
    expected = {
        "schema", "schema_version", "provider_id", "saga_id", "account_id",
        "environment", "scope", "authority_scope", "authority_ref",
        "authority_sha256", "prepared_at", "market_id", "bet_id", "cancel_size",
        "source_order", "semantic_sha256", "customer_ref", "serialization_key",
        "request_sha256",
    }
    if set(value) != expected:
        raise BetfairCancelSagaIntegrityError("cancel intent schema invalid")
    if (
        value["schema"] != "autosport.betfair_cancel_intent"
        or value["schema_version"] != SCHEMA_VERSION
        or value["provider_id"] != _PROVIDER_ID
    ):
        raise BetfairCancelSagaIntegrityError("cancel intent header invalid")
    source_order = (
        None
        if value["source_order"] is None
        else _source_order_from_dict(value["source_order"])
    )
    try:
        intent = CancelIntent(
            saga_id=value["saga_id"],
            account_id=value["account_id"],
            environment=value["environment"],
            scope=CancelScope(value["scope"]),
            authority_scope=CancelAuthorityScope(value["authority_scope"]),
            authority_ref=value["authority_ref"],
            authority_sha256=value["authority_sha256"],
            prepared_at=value["prepared_at"],
            market_id=value["market_id"],
            bet_id=value["bet_id"],
            cancel_size=value["cancel_size"],
            source_order=source_order,
        )
    except (TypeError, ValueError) as exc:
        raise BetfairCancelSagaIntegrityError("cancel intent payload invalid") from exc
    if (
        value["semantic_sha256"] != intent.semantic_sha256
        or value["customer_ref"] != intent.customer_ref
        or value["serialization_key"] != intent.serialization_key
        or value["request_sha256"] != intent.request_sha256
    ):
        raise BetfairCancelSagaIntegrityError("cancel intent derived identity mismatch")
    return intent


def _effect_from_dict(value: object) -> CancelInstructionEffect:
    if type(value) is not dict or set(value) != {
        "bet_id", "size_cancelled", "size_matched", "size_remaining"
    }:
        raise BetfairCancelSagaIntegrityError("cancel provider effect schema invalid")
    try:
        return CancelInstructionEffect(
            value["bet_id"],
            value["size_cancelled"],
            value["size_matched"],
            value["size_remaining"],
        )
    except (TypeError, ValueError) as exc:
        raise BetfairCancelSagaIntegrityError("cancel provider effect invalid") from exc


def _provider_evidence_from_dict(value: object) -> CancelProviderEvidence:
    if type(value) is not dict or set(value) != {
        "evidence_id", "response_sha256", "observed_at", "source",
        "status", "error_code", "effects",
    }:
        raise BetfairCancelSagaIntegrityError("cancel provider evidence schema invalid")
    try:
        return CancelProviderEvidence(
            evidence_id=value["evidence_id"],
            response_sha256=value["response_sha256"],
            observed_at=value["observed_at"],
            source=value["source"],
            status=CancelProviderStatus(value["status"]),
            error_code=value["error_code"],
            effects=tuple(_effect_from_dict(item) for item in value["effects"]),
        )
    except (TypeError, ValueError) as exc:
        raise BetfairCancelSagaIntegrityError("cancel provider evidence invalid") from exc


def _readback_from_dict(value: object) -> CancelOrderReadback:
    if type(value) is not dict or set(value) != {
        "account_id", "market_id", "selection_id", "side", "bet_id", "order_type",
        "size_matched", "size_remaining", "size_cancelled", "size_lapsed", "size_voided",
    }:
        raise BetfairCancelSagaIntegrityError("cancel readback order schema invalid")
    try:
        return CancelOrderReadback(
            account_id=value["account_id"],
            market_id=value["market_id"],
            selection_id=value["selection_id"],
            side=value["side"],
            bet_id=value["bet_id"],
            order_type=CancelOrderType(value["order_type"]),
            size_matched=value["size_matched"],
            size_remaining=value["size_remaining"],
            size_cancelled=value["size_cancelled"],
            size_lapsed=value["size_lapsed"],
            size_voided=value["size_voided"],
        )
    except (TypeError, ValueError) as exc:
        raise BetfairCancelSagaIntegrityError("cancel readback order invalid") from exc


def _reconciliation_from_dict(value: object) -> CancelReconciliationEvidence:
    if type(value) is not dict or set(value) != {
        "evidence_id", "observed_at", "source", "scope_complete", "orders",
        "absent_bet_ids", "final_evidence_sha256",
    }:
        raise BetfairCancelSagaIntegrityError("cancel reconciliation schema invalid")
    try:
        return CancelReconciliationEvidence(
            evidence_id=value["evidence_id"],
            observed_at=value["observed_at"],
            source=value["source"],
            scope_complete=value["scope_complete"],
            orders=tuple(_readback_from_dict(item) for item in value["orders"]),
            absent_bet_ids=tuple(value["absent_bet_ids"]),
            final_evidence_sha256=value["final_evidence_sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise BetfairCancelSagaIntegrityError("cancel reconciliation invalid") from exc


@dataclass(frozen=True, slots=True)
class _Facts:
    submitted_at: str | None
    unknown: dict[str, object] | None
    provider: CancelProviderEvidence | None
    reconciliation: CancelReconciliationEvidence | None


def _exact_single_order(
    intent: CancelIntent,
    order: CancelOrderReadback,
) -> bool:
    source = intent.source_order
    assert source is not None
    return (
        order.account_id == source.account_id
        and order.market_id == source.market_id
        and order.selection_id == source.selection_id
        and order.side == source.side
        and order.bet_id == source.bet_id
        and order.order_type is CancelOrderType.LIMIT
    )


def _provider_effect_for(
    intent: CancelIntent,
    provider: CancelProviderEvidence | None,
) -> CancelInstructionEffect | None:
    if provider is None or intent.scope is not CancelScope.SINGLE_ORDER:
        return None
    for effect in provider.effects:
        if effect.bet_id == intent.bet_id:
            return effect
    return None


def _derive_state_and_exposure(
    intent: CancelIntent,
    facts: _Facts,
) -> tuple[CancelSagaState, CancelExposureView]:
    matched: set[str] = set()
    executable: set[str] = set()
    cancelled: set[str] = set()
    unresolved: set[str] = set()

    if intent.scope is CancelScope.SINGLE_ORDER and intent.source_order is not None:
        if intent.source_order.size_matched > 0:
            matched.add(intent.source_order.bet_id)
        if intent.source_order.size_remaining > 0:
            executable.add(intent.source_order.bet_id)

    provider_effect = _provider_effect_for(intent, facts.provider)
    if provider_effect is not None:
        if provider_effect.size_matched > 0:
            matched.add(provider_effect.bet_id)
        if provider_effect.size_cancelled > 0:
            cancelled.add(provider_effect.bet_id)
        if provider_effect.size_remaining > 0:
            executable.add(provider_effect.bet_id)
        else:
            executable.discard(provider_effect.bet_id)

    reconciliation = facts.reconciliation
    if reconciliation is not None:
        for order in reconciliation.orders:
            if intent.scope is CancelScope.SINGLE_ORDER and not _exact_single_order(intent, order):
                return (
                    CancelSagaState.CONFLICT,
                    CancelExposureView(
                        tuple(sorted(matched)),
                        tuple(sorted(executable)),
                        tuple(sorted(cancelled)),
                        (intent.bet_id,) if intent.bet_id else (),
                        False,
                    ),
                )
            if order.order_type is not CancelOrderType.LIMIT:
                return (
                    CancelSagaState.CONFLICT,
                    CancelExposureView(
                        tuple(sorted(matched)),
                        tuple(sorted(executable)),
                        tuple(sorted(cancelled)),
                        (order.bet_id,),
                        False,
                    ),
                )
            if order.size_matched > 0:
                matched.add(order.bet_id)
            else:
                matched.discard(order.bet_id)
            if order.size_remaining > 0:
                executable.add(order.bet_id)
            else:
                executable.discard(order.bet_id)
            if order.size_cancelled > 0:
                cancelled.add(order.bet_id)

        if intent.scope is CancelScope.SINGLE_ORDER:
            assert intent.bet_id is not None
            matching = [order for order in reconciliation.orders if order.bet_id == intent.bet_id]
            absent = intent.bet_id in reconciliation.absent_bet_ids
            if len(matching) > 1 or (matching and absent):
                return (
                    CancelSagaState.CONFLICT,
                    CancelExposureView(
                        tuple(sorted(matched)),
                        tuple(sorted(executable)),
                        tuple(sorted(cancelled)),
                        (intent.bet_id,),
                        False,
                    ),
                )
            if absent:
                # Current-order disappearance proves only that the bet is no longer
                # present in this scoped current-order read.  An opaque digest of a
                # separate final/cleared artifact cannot prove *why* it disappeared:
                # cancellation, further matching, lapse and void are economically
                # different terminal outcomes.  Until structured final evidence is
                # bound into this contract, keep the saga unresolved even when the
                # current scope is complete and a final_evidence_sha256 is present.
                executable.discard(intent.bet_id)
                unresolved.add(intent.bet_id)
            elif matching:
                current = matching[0]
                source = intent.source_order
                assert source is not None
                if current.size_matched < source.size_matched:
                    return (
                        CancelSagaState.CONFLICT,
                        CancelExposureView(
                            tuple(sorted(matched)),
                            tuple(sorted(executable)),
                            tuple(sorted(cancelled)),
                            (intent.bet_id,),
                            False,
                        ),
                    )
                requested = intent.cancel_size or source.size_remaining
                if (
                    current.size_remaining > source.size_remaining
                    or current.size_cancelled < source.size_cancelled
                ):
                    return (
                        CancelSagaState.CONFLICT,
                        CancelExposureView(
                            tuple(sorted(matched)),
                            tuple(sorted(executable)),
                            tuple(sorted(cancelled)),
                            (intent.bet_id,),
                            False,
                        ),
                    )
                cancelled_delta = current.size_cancelled - source.size_cancelled
                if reconciliation.scope_complete and cancelled_delta >= requested:
                    return (
                        CancelSagaState.CANCEL_RECONCILED,
                        CancelExposureView(
                            tuple(sorted(matched)),
                            tuple(sorted(executable)),
                            tuple(sorted(cancelled)),
                            (),
                            True,
                        ),
                    )
                if (
                    reconciliation.scope_complete
                    and facts.provider is not None
                    and facts.provider.status is CancelProviderStatus.FAILURE
                    and cancelled_delta == 0
                ):
                    return (
                        CancelSagaState.NO_EFFECT_RECONCILED,
                        CancelExposureView(
                            tuple(sorted(matched)),
                            tuple(sorted(executable)),
                            tuple(sorted(cancelled)),
                            (),
                            True,
                        ),
                    )
                if (
                    reconciliation.scope_complete
                    and facts.provider is not None
                    and facts.provider.status is CancelProviderStatus.SUCCESS
                    and cancelled_delta < requested
                ):
                    unresolved.add(intent.bet_id)
                    return (
                        CancelSagaState.CONFLICT,
                        CancelExposureView(
                            tuple(sorted(matched)),
                            tuple(sorted(executable)),
                            tuple(sorted(cancelled)),
                            tuple(sorted(unresolved)),
                            False,
                        ),
                    )
                unresolved.add(intent.bet_id)
            else:
                unresolved.add(intent.bet_id)
        else:
            if reconciliation.scope_complete:
                scope_orders = [
                    order
                    for order in reconciliation.orders
                    if order.account_id == intent.account_id
                    and (
                        intent.scope is CancelScope.ACCOUNT_ALL_LIMIT
                        or order.market_id == intent.market_id
                    )
                ]
                foreign = [
                    order
                    for order in reconciliation.orders
                    if order.account_id != intent.account_id
                    or (
                        intent.scope is CancelScope.MARKET_UNMATCHED
                        and order.market_id != intent.market_id
                    )
                ]
                if foreign:
                    return (
                        CancelSagaState.CONFLICT,
                        CancelExposureView(
                            tuple(sorted(matched)),
                            tuple(sorted(executable)),
                            tuple(sorted(cancelled)),
                            tuple(sorted(order.bet_id for order in foreign)),
                            False,
                        ),
                    )
                remaining = [order.bet_id for order in scope_orders if order.size_remaining > 0]
                if not remaining:
                    return (
                        CancelSagaState.CANCEL_RECONCILED,
                        CancelExposureView(
                            tuple(sorted(matched)),
                            (),
                            tuple(sorted(cancelled)),
                            (),
                            True,
                        ),
                    )
                unresolved.update(remaining)
            else:
                unresolved.update(
                    order.bet_id
                    for order in reconciliation.orders
                    if order.size_remaining > 0
                )

    if facts.submitted_at is None:
        state = CancelSagaState.PREPARED
        retry = ()
    elif facts.provider is not None:
        state = CancelSagaState.PROVIDER_RESULT_UNVERIFIED
        retry = (intent.bet_id,) if intent.bet_id else ()
    else:
        state = CancelSagaState.SUBMITTED_UNKNOWN
        retry = (intent.bet_id,) if intent.bet_id else ()
    unresolved.update(value for value in retry if value)
    return (
        state,
        CancelExposureView(
            tuple(sorted(matched)),
            tuple(sorted(executable)),
            tuple(sorted(cancelled)),
            tuple(sorted(unresolved)),
            False,
        ),
    )


def _retry_disposition(state: CancelSagaState) -> CancelRetryDisposition:
    if state is CancelSagaState.PREPARED:
        return CancelRetryDisposition.EXPLICIT_EXECUTION_POLICY_REQUIRED
    if state in {
        CancelSagaState.SUBMITTED_UNKNOWN,
        CancelSagaState.PROVIDER_RESULT_UNVERIFIED,
        CancelSagaState.UNKNOWN,
    }:
        return CancelRetryDisposition.READBACK_REQUIRED
    if state in {
        CancelSagaState.CANCEL_RECONCILED,
        CancelSagaState.NO_EFFECT_RECONCILED,
    }:
        return CancelRetryDisposition.TERMINAL_NO_RETRY
    return CancelRetryDisposition.CONFLICT_REQUIRES_OPERATOR


class BetfairCancelSagaStore:
    """Append-only hash-chained cancel-saga journal."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _events(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise BetfairCancelSagaIntegrityError("cancel journal has torn final line")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BetfairCancelSagaIntegrityError("cancel journal is not UTF-8") from exc
        events: list[dict[str, object]] = []
        previous = _ZERO_SHA256
        for sequence, line in enumerate(text.splitlines(), 1):
            if not line:
                raise BetfairCancelSagaIntegrityError("cancel journal contains blank line")
            event = _load_json_line(line)
            expected = {
                "schema", "schema_version", "sequence", "saga_id", "event_type",
                "event_at", "previous_sha256", "payload", "event_sha256",
            }
            if set(event) != expected:
                raise BetfairCancelSagaIntegrityError("cancel journal event schema invalid")
            if (
                event["schema"] != "autosport.betfair_cancel_saga_event"
                or event["schema_version"] != SCHEMA_VERSION
                or event["sequence"] != sequence
            ):
                raise BetfairCancelSagaIntegrityError("cancel journal event header invalid")
            try:
                _text(event["saga_id"], "saga_id")
                EventType(event["event_type"])
                canonical_event_at = _timestamp(event["event_at"], "event_at")
                prior = _sha256(event["previous_sha256"], "previous_sha256")
                event_sha = _sha256(event["event_sha256"], "event_sha256")
            except (TypeError, ValueError) as exc:
                raise BetfairCancelSagaIntegrityError("cancel journal event fields invalid") from exc
            if canonical_event_at != event["event_at"]:
                raise BetfairCancelSagaIntegrityError(
                    "cancel journal timestamp is not canonical UTC"
                )
            if prior != previous:
                raise BetfairCancelSagaIntegrityError("cancel journal hash chain is broken")
            unsigned = dict(event)
            del unsigned["event_sha256"]
            if _digest(unsigned) != event_sha:
                raise BetfairCancelSagaIntegrityError("cancel journal event digest mismatch")
            previous = event_sha
            events.append(event)
        self._validate_semantics(events)
        return events

    def _append(
        self,
        event_type: EventType,
        saga_id: str,
        payload: dict[str, object],
        event_at: str,
    ) -> None:
        current = self._events()
        previous = current[-1]["event_sha256"] if current else _ZERO_SHA256
        event = {
            "schema": "autosport.betfair_cancel_saga_event",
            "schema_version": SCHEMA_VERSION,
            "sequence": len(current) + 1,
            "saga_id": _text(saga_id, "saga_id"),
            "event_type": event_type.value,
            "event_at": _timestamp(event_at, "event_at"),
            "previous_sha256": previous,
            "payload": payload,
        }
        event["event_sha256"] = _digest(event)
        encoded = (_canonical(event) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _saga_events(
        events: Iterable[dict[str, object]],
        saga_id: str,
    ) -> list[dict[str, object]]:
        return [event for event in events if event["saga_id"] == saga_id]

    def _intent_from_events(
        self,
        events: list[dict[str, object]],
        saga_id: str,
    ) -> CancelIntent:
        prepared = [
            event
            for event in self._saga_events(events, saga_id)
            if event["event_type"] == EventType.PREPARED.value
        ]
        if len(prepared) != 1:
            raise BetfairCancelSagaIntegrityError(
                "cancel saga must have exactly one PREPARED"
            )
        payload = prepared[0]["payload"]
        if type(payload) is not dict or set(payload) != {"intent"}:
            raise BetfairCancelSagaIntegrityError(
                "cancel PREPARED payload schema invalid"
            )
        return _intent_from_dict(payload["intent"])

    def _facts(self, events: list[dict[str, object]], saga_id: str) -> _Facts:
        saga_events = self._saga_events(events, saga_id)
        submitted = [
            e for e in saga_events if e["event_type"] == EventType.SUBMITTED.value
        ]
        unknowns = [
            e for e in saga_events if e["event_type"] == EventType.UNKNOWN.value
        ]
        providers = [
            e
            for e in saga_events
            if e["event_type"] == EventType.PROVIDER_RESULT.value
        ]
        reconciliations = [
            e
            for e in saga_events
            if e["event_type"] == EventType.RECONCILIATION.value
        ]
        return _Facts(
            submitted_at=(
                submitted[0]["payload"]["submitted_at"] if submitted else None
            ),
            unknown=unknowns[0]["payload"] if unknowns else None,
            provider=(
                _provider_evidence_from_dict(
                    providers[0]["payload"]["evidence"]
                )
                if providers
                else None
            ),
            reconciliation=(
                _reconciliation_from_dict(
                    reconciliations[-1]["payload"]["evidence"]
                )
                if reconciliations
                else None
            ),
        )

    def _validate_semantics(self, events: list[dict[str, object]]) -> None:
        saga_ids: list[str] = []
        for event in events:
            saga_id = event["saga_id"]
            if saga_id not in saga_ids:
                saga_ids.append(saga_id)
        for saga_id in saga_ids:
            saga_events = self._saga_events(events, saga_id)
            intent = self._intent_from_events(events, saga_id)
            if saga_events[0]["event_type"] != EventType.PREPARED.value:
                raise BetfairCancelSagaIntegrityError(
                    "PREPARED must be first saga event"
                )
            if saga_events[0]["event_at"] != intent.prepared_at:
                raise BetfairCancelSagaIntegrityError(
                    "PREPARED timestamp mismatches intent"
                )
            submitted = [
                e
                for e in saga_events
                if e["event_type"] == EventType.SUBMITTED.value
            ]
            unknowns = [
                e
                for e in saga_events
                if e["event_type"] == EventType.UNKNOWN.value
            ]
            providers = [
                e
                for e in saga_events
                if e["event_type"] == EventType.PROVIDER_RESULT.value
            ]
            reconciliations = [
                e
                for e in saga_events
                if e["event_type"] == EventType.RECONCILIATION.value
            ]
            if len(submitted) > 1 or len(unknowns) > 1 or len(providers) > 1:
                raise BetfairCancelSagaIntegrityError(
                    "cancel saga contains duplicate singleton event"
                )
            if providers and not submitted:
                raise BetfairCancelSagaIntegrityError(
                    "provider result requires SUBMITTED boundary"
                )
            if reconciliations and not submitted:
                raise BetfairCancelSagaIntegrityError(
                    "reconciliation requires SUBMITTED boundary"
                )
            boundary = intent.prepared_at
            if submitted:
                payload = submitted[0]["payload"]
                if type(payload) is not dict or set(payload) != {
                    "submitted_at", "request_sha256"
                }:
                    raise BetfairCancelSagaIntegrityError(
                        "cancel SUBMITTED payload schema invalid"
                    )
                submitted_at = _timestamp(
                    payload["submitted_at"],
                    "submitted_at",
                )
                if _time(submitted_at) < _time(intent.prepared_at):
                    raise BetfairCancelSagaIntegrityError(
                        "cancel submission precedes PREPARED"
                    )
                if payload["request_sha256"] != intent.request_sha256:
                    raise BetfairCancelSagaIntegrityError(
                        "submitted request digest mismatches intent"
                    )
                boundary = submitted_at
            if unknowns:
                payload = unknowns[0]["payload"]
                if type(payload) is not dict or set(payload) != {
                    "reason", "observed_at"
                }:
                    raise BetfairCancelSagaIntegrityError(
                        "cancel UNKNOWN payload schema invalid"
                    )
                _text(payload["reason"], "reason")
                observed = _timestamp(
                    payload["observed_at"],
                    "observed_at",
                )
                if _time(observed) < _time(boundary):
                    raise BetfairCancelSagaIntegrityError(
                        "cancel UNKNOWN precedes external boundary"
                    )
            if providers:
                payload = providers[0]["payload"]
                if type(payload) is not dict or set(payload) != {"evidence"}:
                    raise BetfairCancelSagaIntegrityError(
                        "cancel PROVIDER_RESULT payload schema invalid"
                    )
                evidence = _provider_evidence_from_dict(
                    payload["evidence"]
                )
                if _time(evidence.observed_at) < _time(boundary):
                    raise BetfairCancelSagaIntegrityError(
                        "provider result precedes submission"
                    )
                if intent.scope is CancelScope.SINGLE_ORDER:
                    foreign = [
                        effect.bet_id
                        for effect in evidence.effects
                        if effect.bet_id != intent.bet_id
                    ]
                    if foreign:
                        raise BetfairCancelSagaIntegrityError(
                            "provider result contains foreign bet_id"
                        )
            seen_reconciliation: dict[str, dict[str, object]] = {}
            prior_time: datetime | None = None
            for event in reconciliations:
                payload = event["payload"]
                if type(payload) is not dict or set(payload) != {"evidence"}:
                    raise BetfairCancelSagaIntegrityError(
                        "cancel RECONCILIATION payload schema invalid"
                    )
                evidence = _reconciliation_from_dict(
                    payload["evidence"]
                )
                when = _time(evidence.observed_at)
                if when <= _time(boundary):
                    raise BetfairCancelSagaIntegrityError(
                        "reconciliation must be newer than external boundary"
                    )
                if prior_time is not None and when <= prior_time:
                    raise BetfairCancelSagaIntegrityError(
                        "reconciliation evidence must advance observed_at"
                    )
                prior_time = when
                prior = seen_reconciliation.get(evidence.evidence_id)
                if prior is not None and prior != evidence.to_dict():
                    raise BetfairCancelSagaIntegrityError(
                        "reconciliation evidence id has conflicting payload"
                    )
                seen_reconciliation[evidence.evidence_id] = evidence.to_dict()

    def prepare(self, intent: CancelIntent) -> CancelSagaSnapshot:
        if type(intent) is not CancelIntent:
            raise TypeError("intent must be exact CancelIntent")
        with durable_path_lock(self.path):
            events = self._events()
            existing = self._saga_events(events, intent.saga_id)
            if existing:
                stored = self._intent_from_events(
                    events,
                    intent.saga_id,
                )
                if stored != intent:
                    raise BetfairCancelSagaIdentityConflict(
                        "saga_id reused with different immutable cancel intent"
                    )
                return self._snapshot_from_events(
                    events,
                    intent.saga_id,
                )
            self._append(
                EventType.PREPARED,
                intent.saga_id,
                {"intent": intent.to_dict()},
                intent.prepared_at,
            )
            return self.snapshot(intent.saga_id)

    def mark_submitted(
        self,
        saga_id: str,
        *,
        submitted_at: str,
    ) -> CancelSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        canonical_time = _timestamp(submitted_at, "submitted_at")
        with durable_path_lock(self.path):
            events = self._events()
            intent = self._intent_from_events(events, saga)
            facts = self._facts(events, saga)
            if facts.provider is not None or facts.reconciliation is not None:
                raise BetfairCancelSagaStateError(
                    "cannot submit after effect evidence"
                )
            payload = {
                "submitted_at": canonical_time,
                "request_sha256": intent.request_sha256,
            }
            existing = [
                e
                for e in self._saga_events(events, saga)
                if e["event_type"] == EventType.SUBMITTED.value
            ]
            if existing:
                if existing[0]["payload"] == payload:
                    return self._snapshot_from_events(events, saga)
                raise BetfairCancelSagaIdentityConflict(
                    "cancel saga already has a different submission boundary"
                )
            if _time(canonical_time) < _time(intent.prepared_at):
                raise BetfairCancelSagaStateError(
                    "submitted_at precedes prepared_at"
                )
            self._append(
                EventType.SUBMITTED,
                saga,
                payload,
                canonical_time,
            )
            return self.snapshot(saga)

    def mark_unknown(
        self,
        saga_id: str,
        *,
        reason: str,
        observed_at: str,
    ) -> CancelSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        reason_text = _text(reason, "reason")
        canonical_time = _timestamp(observed_at, "observed_at")
        with durable_path_lock(self.path):
            events = self._events()
            self._intent_from_events(events, saga)
            facts = self._facts(events, saga)
            if facts.submitted_at is None:
                raise BetfairCancelSagaStateError(
                    "UNKNOWN requires SUBMITTED boundary"
                )
            if _time(canonical_time) < _time(facts.submitted_at):
                raise BetfairCancelSagaStateError(
                    "UNKNOWN precedes external boundary"
                )
            payload = {
                "reason": reason_text,
                "observed_at": canonical_time,
            }
            existing = [
                e
                for e in self._saga_events(events, saga)
                if e["event_type"] == EventType.UNKNOWN.value
            ]
            if existing:
                if existing[0]["payload"] == payload:
                    return self._snapshot_from_events(events, saga)
                raise BetfairCancelSagaIdentityConflict(
                    "cancel saga already has different uncertainty evidence"
                )
            self._append(
                EventType.UNKNOWN,
                saga,
                payload,
                canonical_time,
            )
            return self.snapshot(saga)

    def record_provider_result(
        self,
        saga_id: str,
        evidence: CancelProviderEvidence,
    ) -> CancelSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        if type(evidence) is not CancelProviderEvidence:
            raise TypeError(
                "evidence must be exact CancelProviderEvidence"
            )
        with durable_path_lock(self.path):
            events = self._events()
            intent = self._intent_from_events(events, saga)
            facts = self._facts(events, saga)
            if facts.submitted_at is None:
                raise BetfairCancelSagaStateError(
                    "provider result requires SUBMITTED boundary"
                )
            if _time(evidence.observed_at) < _time(facts.submitted_at):
                raise BetfairCancelSagaStateError(
                    "provider result precedes submission"
                )
            if intent.scope is CancelScope.SINGLE_ORDER:
                foreign = [
                    effect.bet_id
                    for effect in evidence.effects
                    if effect.bet_id != intent.bet_id
                ]
                if foreign:
                    raise BetfairCancelSagaIdentityConflict(
                        "provider result contains foreign bet_id"
                    )
            payload = {"evidence": evidence.to_dict()}
            existing = [
                e
                for e in self._saga_events(events, saga)
                if e["event_type"] == EventType.PROVIDER_RESULT.value
            ]
            if existing:
                if existing[0]["payload"] == payload:
                    return self._snapshot_from_events(events, saga)
                raise BetfairCancelSagaIdentityConflict(
                    "cancel saga already has different provider result evidence"
                )
            self._append(
                EventType.PROVIDER_RESULT,
                saga,
                payload,
                evidence.observed_at,
            )
            return self.snapshot(saga)

    def record_reconciliation(
        self,
        saga_id: str,
        evidence: CancelReconciliationEvidence,
    ) -> CancelSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        if type(evidence) is not CancelReconciliationEvidence:
            raise TypeError(
                "evidence must be exact CancelReconciliationEvidence"
            )
        with durable_path_lock(self.path):
            events = self._events()
            intent = self._intent_from_events(events, saga)
            facts = self._facts(events, saga)
            if facts.submitted_at is None:
                raise BetfairCancelSagaStateError(
                    "reconciliation requires SUBMITTED boundary"
                )
            if _time(evidence.observed_at) <= _time(facts.submitted_at):
                raise BetfairCancelSagaStateError(
                    "reconciliation must be newer than external boundary"
                )
            if intent.scope is CancelScope.SINGLE_ORDER:
                for order in evidence.orders:
                    if not _exact_single_order(intent, order):
                        raise BetfairCancelSagaIdentityConflict(
                            "single-order readback mismatches exact source identity"
                        )
                foreign_absence = [
                    bet_id
                    for bet_id in evidence.absent_bet_ids
                    if bet_id != intent.bet_id
                ]
                if foreign_absence:
                    raise BetfairCancelSagaIdentityConflict(
                        "single-order reconciliation contains foreign absent bet_id"
                    )
            else:
                for order in evidence.orders:
                    if order.account_id != intent.account_id:
                        raise BetfairCancelSagaIdentityConflict(
                            "scope reconciliation contains foreign account"
                        )
                    if (
                        intent.scope is CancelScope.MARKET_UNMATCHED
                        and order.market_id != intent.market_id
                    ):
                        raise BetfairCancelSagaIdentityConflict(
                            "market reconciliation contains foreign market"
                        )
            existing = [
                e
                for e in self._saga_events(events, saga)
                if e["event_type"] == EventType.RECONCILIATION.value
            ]
            for event in existing:
                prior = _reconciliation_from_dict(
                    event["payload"]["evidence"]
                )
                if prior.evidence_id == evidence.evidence_id:
                    if prior == evidence:
                        return self._snapshot_from_events(events, saga)
                    raise BetfairCancelSagaIdentityConflict(
                        "reconciliation evidence id reused with different payload"
                    )
                if _time(prior.observed_at) >= _time(evidence.observed_at):
                    raise BetfairCancelSagaStateError(
                        "reconciliation evidence must advance observed_at"
                    )
            self._append(
                EventType.RECONCILIATION,
                saga,
                {"evidence": evidence.to_dict()},
                evidence.observed_at,
            )
            return self.snapshot(saga)

    def _snapshot_from_events(
        self,
        events: list[dict[str, object]],
        saga_id: str,
    ) -> CancelSagaSnapshot:
        intent = self._intent_from_events(events, saga_id)
        facts = self._facts(events, saga_id)
        state, exposure = _derive_state_and_exposure(intent, facts)
        raw = self.path.read_bytes() if self.path.exists() else b""
        return CancelSagaSnapshot(
            intent=intent,
            state=state,
            retry_disposition=_retry_disposition(state),
            exposure=exposure,
            event_count=len(self._saga_events(events, saga_id)),
            journal_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def snapshot(self, saga_id: str) -> CancelSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        events = self._events()
        if not self._saga_events(events, saga):
            raise KeyError(saga)
        return self._snapshot_from_events(events, saga)

    def verify_integrity(self) -> int:
        return len(self._events())
