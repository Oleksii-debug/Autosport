"""Crash-safe truth contract for Betfair ``replaceOrders`` composition.

The module deliberately owns no provider transport.  It persists the exact
replace intent before an external write, records the external-call boundary, and
then records provider/reconciliation evidence without pretending that a price
replacement is exposure-atomic.  Betfair documents ``replaceOrders`` as cancel
first and place second: the cancellations are not rolled back if the new orders
cannot be placed.

``RealExecutionLedger`` remains the canonical general execution-effect ledger.
This journal is a narrow composition/recovery authority for the provider-specific
replace saga and never authorizes a provider write, a retry, or real-money use.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Sequence

from .integrity import durable_path_lock
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)

SCHEMA_VERSION = 1
REPLACE_ORDERS_METHOD = "SportsAPING/v1.0/replaceOrders"
_PROVIDER_ID = "betfair"
_MAX_INSTRUCTIONS = 60
_ZERO_SHA256 = "0" * 64
_MONOTONIC_DOMAIN = "provider-execution-replace-saga"
_MONOTONIC_KEY = "betfair-replace-saga-journal-v1"
_MONOTONIC_BINDING_SCHEMA = "autosport.betfair_replace_saga.monotonic_binding"
_MONOTONIC_STATE_SCHEMA = "autosport.betfair_replace_saga.monotonic_state"
_MONOTONIC_TX_SCHEMA = "autosport.betfair_replace_saga.monotonic_tx"


class BetfairReplaceSagaError(RuntimeError):
    """Base error for durable replace-saga operations."""


class BetfairReplaceSagaIntegrityError(BetfairReplaceSagaError):
    """Durable replace evidence is malformed, torn, or internally inconsistent."""


class BetfairReplaceSagaStateError(BetfairReplaceSagaError):
    """A replace-saga transition is unsafe from the current durable state."""


class BetfairReplaceSagaIdentityConflict(BetfairReplaceSagaError):
    """A stable identity was reused with different immutable semantics."""


class ReplacePhaseStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UNKNOWN = "UNKNOWN"


class OriginalOrderState(StrEnum):
    NOT_EXECUTABLE = "NOT_EXECUTABLE"
    EXECUTABLE = "EXECUTABLE"
    UNKNOWN = "UNKNOWN"


class ReplaceSagaState(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTED_UNKNOWN = "SUBMITTED_UNKNOWN"
    UNKNOWN_PARTIAL = "UNKNOWN_PARTIAL"
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
    REPLACE_CANCELLED_WITHOUT_REPLACEMENT = "REPLACE_CANCELLED_WITHOUT_REPLACEMENT"
    REPLACE_REJECTED_OR_CHANGED = "REPLACE_REJECTED_OR_CHANGED"
    REPLACED = "REPLACED"
    CONFLICT = "CONFLICT"


class ReplaceRetryDisposition(StrEnum):
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


def _price(value: object, name: str) -> Decimal:
    if type(value) is Decimal:
        parsed = value
    elif type(value) in {str, int}:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be an exact finite Decimal") from exc
    else:
        raise ValueError(f"{name} must be an exact Decimal-compatible value")
    if not parsed.is_finite() or parsed <= Decimal("1"):
        raise ValueError(f"{name} must be finite and greater than 1")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


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
        raise BetfairReplaceSagaIntegrityError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BetfairReplaceSagaIntegrityError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> None:
    raise BetfairReplaceSagaIntegrityError(f"non-finite JSON number {token!r}")


def _validate_json_tree(value: object, path: str = "value") -> None:
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise BetfairReplaceSagaIntegrityError(
                    f"invalid UTF-8 text at {path}"
                ) from exc
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise BetfairReplaceSagaIntegrityError(
                f"non-finite JSON value at {path}"
            )
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_tree(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise BetfairReplaceSagaIntegrityError(
                    f"non-text JSON object key at {path}"
                )
            _validate_json_tree(key, f"{path} key")
            _validate_json_tree(item, f"{path}.{key}")
        return
    raise BetfairReplaceSagaIntegrityError(
        f"unsupported JSON type {type(value).__name__} at {path}"
    )


@dataclass(frozen=True, slots=True)
class ReplaceInstruction:
    """Exact public Betfair ReplaceInstruction: old ``betId`` + ``newPrice``."""

    bet_id: str
    new_price: Decimal | str | int

    def __post_init__(self) -> None:
        object.__setattr__(self, "bet_id", _text(self.bet_id, "bet_id"))
        object.__setattr__(
            self,
            "new_price",
            _price(self.new_price, "new_price"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"betId": self.bet_id, "newPrice": _decimal_text(self.new_price)}


@dataclass(frozen=True, slots=True)
class ReplaceIntent:
    """Immutable intent persisted before a caller may cross the provider boundary."""

    saga_id: str
    account_id: str
    environment: str
    market_id: str
    authority_ref: str
    authority_sha256: str
    prepared_at: str
    instructions: tuple[ReplaceInstruction, ...]
    market_version: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "saga_id",
            "account_id",
            "environment",
            "market_id",
            "authority_ref",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "authority_sha256",
            _sha256(self.authority_sha256, "authority_sha256"),
        )
        object.__setattr__(self, "prepared_at", _timestamp(self.prepared_at, "prepared_at"))
        instructions = tuple(self.instructions)
        if not instructions or len(instructions) > _MAX_INSTRUCTIONS:
            raise ValueError("replace intent requires 1..60 instructions")
        if not all(type(item) is ReplaceInstruction for item in instructions):
            raise ValueError("instructions must contain exact ReplaceInstruction values")
        if len({item.bet_id for item in instructions}) != len(instructions):
            raise ValueError("replace intent requires unique bet_id values")
        object.__setattr__(self, "instructions", instructions)
        if self.market_version is not None:
            if type(self.market_version) is not int or self.market_version < 0:
                raise ValueError("market_version must be a non-negative int when supplied")

    def _core_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.betfair_replace_intent",
            "schema_version": 1,
            "provider_id": _PROVIDER_ID,
            "saga_id": self.saga_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "market_id": self.market_id,
            "authority_ref": self.authority_ref,
            "authority_sha256": self.authority_sha256,
            "prepared_at": self.prepared_at,
            "market_version": self.market_version,
            "instructions": [item.to_dict() for item in self.instructions],
        }

    @property
    def semantic_sha256(self) -> str:
        return _digest(self._core_payload())

    @property
    def customer_ref(self) -> str:
        """Deterministic request de-dup reference; never a durable order identity."""

        return hashlib.sha256(
            f"autosport:replace:{self.semantic_sha256}".encode("utf-8")
        ).hexdigest()[:32]

    def provider_request(self) -> dict[str, object]:
        params: dict[str, object] = {
            "marketId": self.market_id,
            "instructions": [item.to_dict() for item in self.instructions],
            "customerRef": self.customer_ref,
            "async": False,
        }
        if self.market_version is not None:
            params["marketVersion"] = {"version": self.market_version}
        return {
            "jsonrpc": "2.0",
            "method": REPLACE_ORDERS_METHOD,
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
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True, slots=True)
class ReplaceInstructionResult:
    bet_id: str
    cancel_status: ReplacePhaseStatus
    place_status: ReplacePhaseStatus
    new_bet_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "bet_id", _text(self.bet_id, "bet_id"))
        if type(self.cancel_status) is not ReplacePhaseStatus:
            raise ValueError("cancel_status must be ReplacePhaseStatus")
        if type(self.place_status) is not ReplacePhaseStatus:
            raise ValueError("place_status must be ReplacePhaseStatus")
        if self.new_bet_id is not None:
            object.__setattr__(self, "new_bet_id", _text(self.new_bet_id, "new_bet_id"))
        if self.place_status is ReplacePhaseStatus.SUCCESS and self.new_bet_id is None:
            raise ValueError("successful replacement placement requires new_bet_id")
        if self.place_status is not ReplacePhaseStatus.SUCCESS and self.new_bet_id is not None:
            raise ValueError("non-successful placement cannot claim new_bet_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "bet_id": self.bet_id,
            "cancel_status": self.cancel_status.value,
            "place_status": self.place_status.value,
            "new_bet_id": self.new_bet_id,
        }


@dataclass(frozen=True, slots=True)
class ReplaceProviderEvidence:
    evidence_id: str
    response_sha256: str
    observed_at: str
    source: str
    results: tuple[ReplaceInstructionResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _sha256(self.evidence_id, "evidence_id"))
        object.__setattr__(
            self,
            "response_sha256",
            _sha256(self.response_sha256, "response_sha256"),
        )
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        results = tuple(self.results)
        if not results or not all(type(item) is ReplaceInstructionResult for item in results):
            raise ValueError("results must contain exact ReplaceInstructionResult values")
        if len({item.bet_id for item in results}) != len(results):
            raise ValueError("provider result requires unique bet_id values")
        successful_ids = [item.new_bet_id for item in results if item.new_bet_id is not None]
        if len(set(successful_ids)) != len(successful_ids):
            raise ValueError("provider result requires unique replacement bet ids")
        object.__setattr__(self, "results", results)

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "response_sha256": self.response_sha256,
            "observed_at": self.observed_at,
            "source": self.source,
            "results": [item.to_dict() for item in self.results],
        }


@dataclass(frozen=True, slots=True)
class ReplaceInstructionReconciliation:
    bet_id: str
    original_state: OriginalOrderState
    new_bet_id: str | None
    linkage_sha256: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "bet_id", _text(self.bet_id, "bet_id"))
        if type(self.original_state) is not OriginalOrderState:
            raise ValueError("original_state must be OriginalOrderState")
        if self.new_bet_id is None:
            if self.linkage_sha256 is not None:
                raise ValueError("linkage_sha256 requires new_bet_id")
        else:
            object.__setattr__(self, "new_bet_id", _text(self.new_bet_id, "new_bet_id"))
            if self.linkage_sha256 is None:
                raise ValueError("new_bet_id requires explicit linkage_sha256")
            object.__setattr__(
                self,
                "linkage_sha256",
                _sha256(self.linkage_sha256, "linkage_sha256"),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "bet_id": self.bet_id,
            "original_state": self.original_state.value,
            "new_bet_id": self.new_bet_id,
            "linkage_sha256": self.linkage_sha256,
        }


@dataclass(frozen=True, slots=True)
class ReplaceReconciliationEvidence:
    evidence_id: str
    observed_at: str
    source: str
    results: tuple[ReplaceInstructionReconciliation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _sha256(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        results = tuple(self.results)
        if not results or not all(
            type(item) is ReplaceInstructionReconciliation for item in results
        ):
            raise ValueError(
                "results must contain exact ReplaceInstructionReconciliation values"
            )
        if len({item.bet_id for item in results}) != len(results):
            raise ValueError("reconciliation requires unique bet_id values")
        new_ids = [item.new_bet_id for item in results if item.new_bet_id is not None]
        if len(set(new_ids)) != len(new_ids):
            raise ValueError("reconciliation requires unique replacement bet ids")
        object.__setattr__(self, "results", results)

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at,
            "source": self.source,
            "results": [item.to_dict() for item in self.results],
        }


@dataclass(frozen=True, slots=True)
class ReplaceExposureTruth:
    saga_id: str
    state: ReplaceSagaState
    known_not_executable_original_bet_ids: tuple[str, ...]
    known_executable_original_bet_ids: tuple[str, ...]
    known_replacement_bet_ids: tuple[str, ...]
    unresolved_original_bet_ids: tuple[str, ...]
    unresolved_replacement_bet_ids_for: tuple[str, ...]
    request_sha256: str
    provider_verified: bool = field(default=False, init=False)
    execution_admission_eligible: bool = field(default=False, init=False)
    real_money_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "saga_id", _text(self.saga_id, "saga_id"))
        if type(self.state) is not ReplaceSagaState:
            raise ValueError("state must be ReplaceSagaState")
        tuple_fields = (
            "known_not_executable_original_bet_ids",
            "known_executable_original_bet_ids",
            "known_replacement_bet_ids",
            "unresolved_original_bet_ids",
            "unresolved_replacement_bet_ids_for",
        )
        for name in tuple_fields:
            value = getattr(self, name)
            if type(value) is not tuple or not all(type(item) is str for item in value):
                raise ValueError(f"{name} must be a tuple of canonical text values")
            canonical = tuple(_text(item, name) for item in value)
            if len(set(canonical)) != len(canonical):
                raise ValueError(f"{name} must not contain duplicates")
            object.__setattr__(self, name, canonical)
        if set(self.known_not_executable_original_bet_ids) & set(
            self.known_executable_original_bet_ids
        ):
            raise ValueError("original bet cannot be both executable and not executable")
        object.__setattr__(
            self,
            "request_sha256",
            _sha256(self.request_sha256, "request_sha256"),
        )


@dataclass(frozen=True, slots=True)
class ReplaceSagaSnapshot:
    intent: ReplaceIntent
    state: ReplaceSagaState
    retry_disposition: ReplaceRetryDisposition
    exposure: ReplaceExposureTruth
    event_count: int
    journal_sha256: str


@dataclass(frozen=True, slots=True)
class _Facts:
    submitted_at: str | None
    unknown_at: str | None
    provider: ReplaceProviderEvidence | None
    reconciliation: ReplaceReconciliationEvidence | None
    conflict_observed: bool


def _intent_from_dict(value: object) -> ReplaceIntent:
    if type(value) is not dict:
        raise BetfairReplaceSagaIntegrityError("stored replace intent must be an object")
    expected = {
        "schema",
        "schema_version",
        "provider_id",
        "saga_id",
        "account_id",
        "environment",
        "market_id",
        "authority_ref",
        "authority_sha256",
        "prepared_at",
        "market_version",
        "instructions",
        "semantic_sha256",
        "customer_ref",
        "request_sha256",
    }
    if set(value) != expected:
        raise BetfairReplaceSagaIntegrityError("stored replace intent schema is invalid")
    if (
        value["schema"] != "autosport.betfair_replace_intent"
        or value["schema_version"] != 1
        or value["provider_id"] != _PROVIDER_ID
        or type(value["instructions"]) is not list
    ):
        raise BetfairReplaceSagaIntegrityError("stored replace intent header is invalid")
    try:
        instructions = tuple(
            ReplaceInstruction(
                bet_id=item["betId"],
                new_price=item["newPrice"],
            )
            for item in value["instructions"]
            if type(item) is dict and set(item) == {"betId", "newPrice"}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BetfairReplaceSagaIntegrityError("stored replace instructions are invalid") from exc
    if len(instructions) != len(value["instructions"]):
        raise BetfairReplaceSagaIntegrityError("stored replace instruction schema is invalid")
    try:
        intent = ReplaceIntent(
            saga_id=value["saga_id"],
            account_id=value["account_id"],
            environment=value["environment"],
            market_id=value["market_id"],
            authority_ref=value["authority_ref"],
            authority_sha256=value["authority_sha256"],
            prepared_at=value["prepared_at"],
            instructions=instructions,
            market_version=value["market_version"],
        )
    except (TypeError, ValueError) as exc:
        raise BetfairReplaceSagaIntegrityError("stored replace intent values are invalid") from exc
    if intent.to_dict() != value:
        raise BetfairReplaceSagaIntegrityError("stored replace intent is not canonical")
    return intent


def _provider_evidence_from_dict(value: object) -> ReplaceProviderEvidence:
    if type(value) is not dict or set(value) != {
        "evidence_id",
        "response_sha256",
        "observed_at",
        "source",
        "results",
    }:
        raise BetfairReplaceSagaIntegrityError("stored provider evidence schema is invalid")
    if type(value["results"]) is not list:
        raise BetfairReplaceSagaIntegrityError("stored provider results are invalid")
    results: list[ReplaceInstructionResult] = []
    try:
        for item in value["results"]:
            if type(item) is not dict or set(item) != {
                "bet_id",
                "cancel_status",
                "place_status",
                "new_bet_id",
            }:
                raise BetfairReplaceSagaIntegrityError(
                    "stored provider instruction result schema is invalid"
                )
            results.append(
                ReplaceInstructionResult(
                    bet_id=item["bet_id"],
                    cancel_status=ReplacePhaseStatus(item["cancel_status"]),
                    place_status=ReplacePhaseStatus(item["place_status"]),
                    new_bet_id=item["new_bet_id"],
                )
            )
        evidence = ReplaceProviderEvidence(
            evidence_id=value["evidence_id"],
            response_sha256=value["response_sha256"],
            observed_at=value["observed_at"],
            source=value["source"],
            results=tuple(results),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BetfairReplaceSagaIntegrityError("stored provider evidence values are invalid") from exc
    if evidence.to_dict() != value:
        raise BetfairReplaceSagaIntegrityError("stored provider evidence is not canonical")
    return evidence


def _reconciliation_from_dict(value: object) -> ReplaceReconciliationEvidence:
    if type(value) is not dict or set(value) != {
        "evidence_id",
        "observed_at",
        "source",
        "results",
    }:
        raise BetfairReplaceSagaIntegrityError("stored reconciliation schema is invalid")
    if type(value["results"]) is not list:
        raise BetfairReplaceSagaIntegrityError("stored reconciliation results are invalid")
    results: list[ReplaceInstructionReconciliation] = []
    try:
        for item in value["results"]:
            if type(item) is not dict or set(item) != {
                "bet_id",
                "original_state",
                "new_bet_id",
                "linkage_sha256",
            }:
                raise BetfairReplaceSagaIntegrityError(
                    "stored reconciliation item schema is invalid"
                )
            results.append(
                ReplaceInstructionReconciliation(
                    bet_id=item["bet_id"],
                    original_state=OriginalOrderState(item["original_state"]),
                    new_bet_id=item["new_bet_id"],
                    linkage_sha256=item["linkage_sha256"],
                )
            )
        evidence = ReplaceReconciliationEvidence(
            evidence_id=value["evidence_id"],
            observed_at=value["observed_at"],
            source=value["source"],
            results=tuple(results),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BetfairReplaceSagaIntegrityError("stored reconciliation values are invalid") from exc
    if evidence.to_dict() != value:
        raise BetfairReplaceSagaIntegrityError("stored reconciliation is not canonical")
    return evidence


def _ordered_ids(intent: ReplaceIntent) -> tuple[str, ...]:
    return tuple(item.bet_id for item in intent.instructions)


def _same_id_set(intent: ReplaceIntent, items: Iterable[object], *, attr: str) -> bool:
    values = [getattr(item, attr) for item in items]
    return len(values) == len(intent.instructions) and set(values) == set(_ordered_ids(intent))


def _derive_state_and_exposure(intent: ReplaceIntent, facts: _Facts) -> tuple[ReplaceSagaState, ReplaceExposureTruth]:
    originals = _ordered_ids(intent)
    provider = facts.provider
    reconciliation = facts.reconciliation

    if provider is None and reconciliation is None:
        state = (
            ReplaceSagaState.PREPARED
            if facts.submitted_at is None and facts.unknown_at is None
            else ReplaceSagaState.SUBMITTED_UNKNOWN
        )
        exposure = ReplaceExposureTruth(
            intent.saga_id,
            state,
            (),
            (),
            (),
            originals,
            originals if facts.submitted_at is not None or facts.unknown_at is not None else (),
            intent.request_sha256,
        )
        return state, exposure

    known_not_executable: set[str] = set()
    known_executable: set[str] = set()
    known_new: dict[str, str] = {}
    unresolved_original: set[str] = set(originals)
    unresolved_replacement: set[str] = set(originals)
    # Conflict is a durable high-water safety incident. A later ordinary
    # reconciliation may refine the current exposure, but cannot retroactively
    # erase an already-observed impossible order/linkage state. This module has
    # no conflict-resolution authority, so such a conflict remains sticky.
    conflict = facts.conflict_observed

    if provider is not None:
        for result in provider.results:
            if result.cancel_status is ReplacePhaseStatus.SUCCESS:
                known_not_executable.add(result.bet_id)
                unresolved_original.discard(result.bet_id)
            elif result.cancel_status is ReplacePhaseStatus.FAILURE:
                # Cancellation failure does not prove the original remains executable.
                pass
            if result.place_status is ReplacePhaseStatus.SUCCESS:
                assert result.new_bet_id is not None
                known_new[result.bet_id] = result.new_bet_id
                unresolved_replacement.discard(result.bet_id)
            elif result.place_status is ReplacePhaseStatus.FAILURE:
                unresolved_replacement.discard(result.bet_id)
            if (
                result.cancel_status is ReplacePhaseStatus.FAILURE
                and result.place_status is ReplacePhaseStatus.SUCCESS
            ):
                conflict = True

        placed = [item.place_status for item in provider.results]
        if ReplacePhaseStatus.SUCCESS in placed and ReplacePhaseStatus.FAILURE in placed:
            # Betfair documents the new-order placement phase as all-or-none.
            conflict = True

    if reconciliation is not None:
        for item in reconciliation.results:
            if item.original_state is OriginalOrderState.NOT_EXECUTABLE:
                known_not_executable.add(item.bet_id)
                known_executable.discard(item.bet_id)
                unresolved_original.discard(item.bet_id)
            elif item.original_state is OriginalOrderState.EXECUTABLE:
                known_executable.add(item.bet_id)
                known_not_executable.discard(item.bet_id)
                unresolved_original.discard(item.bet_id)
            else:
                unresolved_original.add(item.bet_id)
            if item.new_bet_id is not None:
                prior_new = known_new.get(item.bet_id)
                if prior_new is not None and prior_new != item.new_bet_id:
                    conflict = True
                known_new[item.bet_id] = item.new_bet_id
                unresolved_replacement.discard(item.bet_id)
            if item.original_state is OriginalOrderState.EXECUTABLE and item.bet_id in known_new:
                conflict = True

    if len(set(known_new.values())) != len(known_new):
        conflict = True
    if any(bet_id in known_new for bet_id in known_executable):
        conflict = True
    if provider is not None and reconciliation is not None:
        provider_by_id = {item.bet_id: item for item in provider.results}
        reconciliation_by_id = {item.bet_id: item for item in reconciliation.results}
        for bet_id in originals:
            provider_item = provider_by_id[bet_id]
            reconciled_item = reconciliation_by_id[bet_id]
            if (
                provider_item.place_status is ReplacePhaseStatus.FAILURE
                and reconciled_item.new_bet_id is not None
            ):
                conflict = True
            if (
                provider_item.cancel_status is ReplacePhaseStatus.SUCCESS
                and reconciled_item.original_state is OriginalOrderState.EXECUTABLE
            ):
                conflict = True

    all_original_not_executable = set(originals) == known_not_executable and not known_executable
    all_replacements_known = set(originals) == set(known_new)
    any_unknown_provider = provider is not None and any(
        item.cancel_status is ReplacePhaseStatus.UNKNOWN
        or item.place_status is ReplacePhaseStatus.UNKNOWN
        for item in provider.results
    )

    if conflict:
        state = ReplaceSagaState.CONFLICT
    elif all_original_not_executable and not all_replacements_known:
        # Caller-carried provider/readback DTOs are observations, not product-
        # issued provider truth. They may conservatively establish that an old
        # order is no longer executable, but cannot prove terminal replacement
        # failure or suppress required readback.
        state = ReplaceSagaState.CANCEL_CONFIRMED
    else:
        # Until this saga consumes a canonical product-owned provider/readback
        # evidence capability, even apparently complete positive observations
        # remain nonterminal and require reconciliation.
        state = ReplaceSagaState.UNKNOWN_PARTIAL

    exposure = ReplaceExposureTruth(
        saga_id=intent.saga_id,
        state=state,
        known_not_executable_original_bet_ids=tuple(sorted(known_not_executable)),
        known_executable_original_bet_ids=tuple(sorted(known_executable)),
        known_replacement_bet_ids=tuple(sorted(known_new.values())),
        unresolved_original_bet_ids=tuple(sorted(unresolved_original)),
        unresolved_replacement_bet_ids_for=tuple(sorted(unresolved_replacement)),
        request_sha256=intent.request_sha256,
    )
    return state, exposure


def _retry_disposition(state: ReplaceSagaState) -> ReplaceRetryDisposition:
    if state is ReplaceSagaState.PREPARED:
        return ReplaceRetryDisposition.EXPLICIT_EXECUTION_POLICY_REQUIRED
    if state in {
        ReplaceSagaState.SUBMITTED_UNKNOWN,
        ReplaceSagaState.UNKNOWN_PARTIAL,
        ReplaceSagaState.CANCEL_CONFIRMED,
    }:
        return ReplaceRetryDisposition.READBACK_REQUIRED
    if state is ReplaceSagaState.CONFLICT:
        return ReplaceRetryDisposition.CONFLICT_REQUIRES_OPERATOR
    return ReplaceRetryDisposition.TERMINAL_NO_RETRY


class BetfairReplaceSagaStore:
    """Append-only, hash-chained replace-saga journal with no provider I/O."""

    _EVENT_FIELDS = frozenset(
        {
            "schema_version",
            "event_id",
            "event_type",
            "recorded_at",
            "saga_id",
            "prev_sha256",
            "payload",
        }
    )

    def __init__(self, path: str | Path) -> None:
        # Freeze the lexical location once. A later cwd change must not switch
        # the journal lock or monotonic authority namespace.
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _absolute_path(self) -> Path:
        # Preserve lexical path identity. MonotonicWorkspaceAuthority deliberately
        # reserves lexical workspace locations so symlink/junction retargeting
        # cannot silently select a fresh ancestry.
        return self.path

    def _monotonic_path_identity(self) -> str:
        return os.path.normcase(self._absolute_path().name)

    def _monotonic_authority(self) -> MonotonicWorkspaceAuthority:
        absolute = self._absolute_path()
        return MonotonicWorkspaceAuthority(
            workspace=absolute.parent,
            domain=_MONOTONIC_DOMAIN,
            key=_MONOTONIC_KEY,
        )

    def _monotonic_binding(self) -> str:
        return _digest(
            {
                "schema": _MONOTONIC_BINDING_SCHEMA,
                "schema_version": 1,
                "provider_id": _PROVIDER_ID,
                "journal_schema_version": SCHEMA_VERSION,
                "journal_path_identity": self._monotonic_path_identity(),
            }
        )

    def _monotonic_state_digest(self, raw: bytes) -> str | None:
        if not raw:
            return None
        return _digest(
            {
                "schema": _MONOTONIC_STATE_SCHEMA,
                "schema_version": 1,
                "provider_id": _PROVIDER_ID,
                "journal_path_identity": self._monotonic_path_identity(),
                "journal_size": len(raw),
                "journal_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    @staticmethod
    def _raise_monotonic_error(exc: MonotonicWorkspaceAuthorityError) -> None:
        raise BetfairReplaceSagaIntegrityError(
            f"replace journal monotonic authority rejected local state: {exc}"
        ) from exc

    def _ensure_monotonic_current(self, raw: bytes) -> None:
        observed = self._monotonic_state_digest(raw)
        binding = self._monotonic_binding()
        try:
            authority = self._monotonic_authority()
            history = authority.read_history()
            if history and history[-1].phase is AuthorityPhase.PREPARE:
                pending = history[-1]
                if pending.semantic_binding_sha256 != binding:
                    raise BetfairReplaceSagaIntegrityError(
                        "replace journal has a pending transaction for a different path binding"
                    )
                authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                authority.recover(observed_state_sha256=observed)
        except BetfairReplaceSagaError:
            raise
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    @staticmethod
    def _validate_event(event: object, *, line: int | None = None) -> dict[str, object]:
        where = f" at line {line}" if line is not None else ""
        if type(event) is not dict or set(event) != BetfairReplaceSagaStore._EVENT_FIELDS:
            raise BetfairReplaceSagaIntegrityError(f"replace event schema invalid{where}")
        if type(event["schema_version"]) is not int or event["schema_version"] != SCHEMA_VERSION:
            raise BetfairReplaceSagaIntegrityError(f"unsupported replace event schema{where}")
        try:
            _text(event["event_id"], "event_id")
            EventType(event["event_type"])
            _timestamp(event["recorded_at"], "recorded_at")
            _text(event["saga_id"], "saga_id")
            _sha256(event["prev_sha256"], "prev_sha256")
        except (TypeError, ValueError) as exc:
            raise BetfairReplaceSagaIntegrityError(f"replace event header invalid{where}") from exc
        if type(event["payload"]) is not dict:
            raise BetfairReplaceSagaIntegrityError(f"replace event payload invalid{where}")
        _validate_json_tree(event)
        return event

    @classmethod
    def _parse(cls, raw: bytes) -> list[dict[str, object]]:
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise BetfairReplaceSagaIntegrityError("replace journal has unterminated final event")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BetfairReplaceSagaIntegrityError("replace journal is not valid UTF-8") from exc
        events: list[dict[str, object]] = []
        event_ids: set[str] = set()
        previous = _ZERO_SHA256
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line:
                raise BetfairReplaceSagaIntegrityError(f"blank journal line {line_number}")
            try:
                envelope = json.loads(
                    line,
                    object_pairs_hook=_strict_object_pairs,
                    parse_constant=_reject_nonfinite,
                )
            except json.JSONDecodeError as exc:
                raise BetfairReplaceSagaIntegrityError(
                    f"invalid journal JSON at line {line_number}"
                ) from exc
            if type(envelope) is not dict or set(envelope) != {"sha256", "event"}:
                raise BetfairReplaceSagaIntegrityError(
                    f"invalid journal envelope at line {line_number}"
                )
            try:
                digest = _sha256(envelope["sha256"], "sha256")
            except ValueError as exc:
                raise BetfairReplaceSagaIntegrityError(
                    f"invalid event digest at line {line_number}"
                ) from exc
            event = cls._validate_event(envelope["event"], line=line_number)
            if event["prev_sha256"] != previous:
                raise BetfairReplaceSagaIntegrityError(
                    f"replace journal chain break at line {line_number}"
                )
            expected = _digest(event)
            if digest != expected:
                raise BetfairReplaceSagaIntegrityError(
                    f"replace journal digest mismatch at line {line_number}"
                )
            event_id = event["event_id"]
            if event_id in event_ids:
                raise BetfairReplaceSagaIntegrityError(
                    f"duplicate event_id at line {line_number}"
                )
            event_ids.add(event_id)
            previous = digest
            events.append(event)
        cls._validate_semantics(events)
        return events

    def _events(self) -> list[dict[str, object]]:
        raw = self.path.read_bytes() if self.path.exists() else b""
        events = self._parse(raw)
        self._ensure_monotonic_current(raw)
        return events

    def _append(
        self,
        kind: EventType,
        saga_id: str,
        payload: dict[str, object],
        recorded_at: str,
    ) -> None:
        events = self._events()
        previous = _digest(events[-1]) if events else _ZERO_SHA256
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "event_type": kind.value,
            "recorded_at": _timestamp(recorded_at, "recorded_at"),
            "saga_id": _text(saga_id, "saga_id"),
            "prev_sha256": previous,
            "payload": payload,
        }
        self._validate_event(event)
        envelope = (
            _canonical({"sha256": _digest(event), "event": event}) + "\n"
        ).encode("utf-8")

        current_raw = self.path.read_bytes() if self.path.exists() else b""
        if self._parse(current_raw) != events:
            raise BetfairReplaceSagaIntegrityError(
                "replace journal changed during a locked append"
            )
        observed_state = self._monotonic_state_digest(current_raw)
        intended_raw = current_raw + envelope
        intended_state = self._monotonic_state_digest(intended_raw)
        assert intended_state is not None
        binding = self._monotonic_binding()
        path_existed = self.path.exists()

        try:
            authority = self._monotonic_authority()
            history = authority.read_history()
            authority_tip = None if not history else history[-1].record_sha256
            tx_id = _digest(
                {
                    "schema": _MONOTONIC_TX_SCHEMA,
                    "schema_version": 1,
                    "provider_id": _PROVIDER_ID,
                    "journal_path_identity": self._monotonic_path_identity(),
                    "observed_state_sha256": observed_state,
                    "intended_state_sha256": intended_state,
                    "event_sha256": _digest(event),
                    "authority_tip_sha256": authority_tip,
                }
            )
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed_state,
                intended_state_sha256=intended_state,
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

        try:
            with self.path.open("ab") as handle:
                handle.write(envelope)
                handle.flush()
                os.fsync(handle.fileno())
            if not path_existed and os.name != "nt":
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                fd = os.open(self.path.parent, flags)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            raise BetfairReplaceSagaIntegrityError(
                "replace journal durability barrier failed"
            ) from exc

        published_raw = self.path.read_bytes()
        if published_raw != intended_raw:
            raise BetfairReplaceSagaIntegrityError(
                "replace journal publication differs from monotonic PREPARE"
            )
        published_state = self._monotonic_state_digest(published_raw)
        if published_state != intended_state:
            raise BetfairReplaceSagaIntegrityError(
                "replace journal state digest differs from monotonic PREPARE"
            )
        try:
            authority.commit(
                tx_id=tx_id,
                observed_state_sha256=intended_state,
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    @staticmethod
    def _saga_events(events: Sequence[dict[str, object]], saga_id: str) -> list[dict[str, object]]:
        return [event for event in events if event["saga_id"] == saga_id]

    @staticmethod
    def _intent_from_events(events: Sequence[dict[str, object]], saga_id: str) -> ReplaceIntent:
        saga_events = BetfairReplaceSagaStore._saga_events(events, saga_id)
        prepared = [event for event in saga_events if event["event_type"] == EventType.PREPARED.value]
        if len(prepared) != 1:
            raise BetfairReplaceSagaIntegrityError(
                "replace saga must contain exactly one PREPARED event"
            )
        return _intent_from_dict(prepared[0]["payload"]["intent"])

    @staticmethod
    def _facts(events: Sequence[dict[str, object]], saga_id: str) -> _Facts:
        saga_events = BetfairReplaceSagaStore._saga_events(events, saga_id)
        submitted = [event for event in saga_events if event["event_type"] == EventType.SUBMITTED.value]
        unknown = [event for event in saga_events if event["event_type"] == EventType.UNKNOWN.value]
        provider_events = [
            event
            for event in saga_events
            if event["event_type"] == EventType.PROVIDER_RESULT.value
        ]
        reconciliation_events = [
            event
            for event in saga_events
            if event["event_type"] == EventType.RECONCILIATION.value
        ]
        provider = (
            _provider_evidence_from_dict(provider_events[0]["payload"]["evidence"])
            if provider_events
            else None
        )
        reconciliations = [
            _reconciliation_from_dict(event["payload"]["evidence"])
            for event in reconciliation_events
        ]

        conflict_observed = False
        if provider is not None:
            placed = [item.place_status for item in provider.results]
            if (
                ReplacePhaseStatus.SUCCESS in placed
                and ReplacePhaseStatus.FAILURE in placed
            ):
                conflict_observed = True
            if any(
                item.cancel_status is ReplacePhaseStatus.FAILURE
                and item.place_status is ReplacePhaseStatus.SUCCESS
                for item in provider.results
            ):
                conflict_observed = True

        prior_new_by_original: dict[str, str] = {}
        provider_by_id = (
            {item.bet_id: item for item in provider.results}
            if provider is not None
            else {}
        )
        for evidence in reconciliations:
            for item in evidence.results:
                if (
                    item.original_state is OriginalOrderState.EXECUTABLE
                    and item.new_bet_id is not None
                ):
                    conflict_observed = True
                if item.new_bet_id is not None:
                    prior_new = prior_new_by_original.get(item.bet_id)
                    if prior_new is not None and prior_new != item.new_bet_id:
                        conflict_observed = True
                    prior_new_by_original[item.bet_id] = item.new_bet_id

                provider_item = provider_by_id.get(item.bet_id)
                if provider_item is None:
                    continue
                if (
                    provider_item.place_status is ReplacePhaseStatus.FAILURE
                    and item.new_bet_id is not None
                ):
                    conflict_observed = True
                if (
                    provider_item.place_status is ReplacePhaseStatus.SUCCESS
                    and item.new_bet_id is not None
                    and provider_item.new_bet_id != item.new_bet_id
                ):
                    conflict_observed = True
                if (
                    provider_item.cancel_status is ReplacePhaseStatus.SUCCESS
                    and item.original_state is OriginalOrderState.EXECUTABLE
                ):
                    conflict_observed = True

        return _Facts(
            submitted_at=submitted[0]["payload"]["submitted_at"] if submitted else None,
            unknown_at=unknown[0]["payload"]["observed_at"] if unknown else None,
            provider=provider,
            reconciliation=reconciliations[-1] if reconciliations else None,
            conflict_observed=conflict_observed,
        )

    @classmethod
    def _validate_semantics(cls, events: list[dict[str, object]]) -> None:
        saga_ids = []
        for event in events:
            saga_id = event["saga_id"]
            if saga_id not in saga_ids:
                saga_ids.append(saga_id)
        for saga_id in saga_ids:
            saga_events = cls._saga_events(events, saga_id)
            if not saga_events or saga_events[0]["event_type"] != EventType.PREPARED.value:
                raise BetfairReplaceSagaIntegrityError(
                    "replace saga must begin with PREPARED"
                )
            prepared = [e for e in saga_events if e["event_type"] == EventType.PREPARED.value]
            if len(prepared) != 1 or set(prepared[0]["payload"]) != {"intent"}:
                raise BetfairReplaceSagaIntegrityError(
                    "replace saga has invalid PREPARED multiplicity/schema"
                )
            intent = _intent_from_dict(prepared[0]["payload"]["intent"])
            if prepared[0]["saga_id"] != intent.saga_id:
                raise BetfairReplaceSagaIntegrityError(
                    "PREPARED event saga_id mismatches immutable intent"
                )
            if _timestamp(prepared[0]["recorded_at"], "recorded_at") != intent.prepared_at:
                raise BetfairReplaceSagaIntegrityError(
                    "PREPARED recorded_at mismatches intent prepared_at"
                )
            submitted_events = [e for e in saga_events if e["event_type"] == EventType.SUBMITTED.value]
            if len(submitted_events) > 1:
                raise BetfairReplaceSagaIntegrityError("replace saga has multiple SUBMITTED events")
            submitted_at: str | None = None
            submitted_index: int | None = None
            if submitted_events:
                event = submitted_events[0]
                submitted_index = saga_events.index(event)
                if set(event["payload"]) != {"submitted_at", "request_sha256"}:
                    raise BetfairReplaceSagaIntegrityError("replace SUBMITTED payload schema invalid")
                try:
                    submitted_at = _timestamp(event["payload"]["submitted_at"], "submitted_at")
                    request_sha = _sha256(event["payload"]["request_sha256"], "request_sha256")
                except ValueError as exc:
                    raise BetfairReplaceSagaIntegrityError("replace SUBMITTED payload invalid") from exc
                if request_sha != intent.request_sha256:
                    raise BetfairReplaceSagaIntegrityError("submitted request digest mismatches prepared intent")
                if _timestamp(event["recorded_at"], "recorded_at") != submitted_at:
                    raise BetfairReplaceSagaIntegrityError(
                        "SUBMITTED recorded_at mismatches submitted_at"
                    )
                if _time(submitted_at) < _time(intent.prepared_at):
                    raise BetfairReplaceSagaIntegrityError("replace submission precedes PREPARED")
            unknown_events = [e for e in saga_events if e["event_type"] == EventType.UNKNOWN.value]
            if len(unknown_events) > 1:
                raise BetfairReplaceSagaIntegrityError("replace saga has multiple UNKNOWN events")
            if unknown_events:
                event = unknown_events[0]
                if set(event["payload"]) != {"reason", "observed_at"}:
                    raise BetfairReplaceSagaIntegrityError("replace UNKNOWN payload schema invalid")
                try:
                    _text(event["payload"]["reason"], "reason")
                    observed = _timestamp(event["payload"]["observed_at"], "observed_at")
                except ValueError as exc:
                    raise BetfairReplaceSagaIntegrityError("replace UNKNOWN payload invalid") from exc
                if _timestamp(event["recorded_at"], "recorded_at") != observed:
                    raise BetfairReplaceSagaIntegrityError(
                        "UNKNOWN recorded_at mismatches observed_at"
                    )
                if submitted_at is None or submitted_index is None:
                    raise BetfairReplaceSagaIntegrityError(
                        "replace UNKNOWN requires prior SUBMITTED boundary"
                    )
                if saga_events.index(event) <= submitted_index:
                    raise BetfairReplaceSagaIntegrityError(
                        "replace UNKNOWN must follow SUBMITTED in journal order"
                    )
                if _time(observed) < _time(submitted_at):
                    raise BetfairReplaceSagaIntegrityError(
                        "replace UNKNOWN precedes causal boundary"
                    )
            provider_events = [e for e in saga_events if e["event_type"] == EventType.PROVIDER_RESULT.value]
            if len(provider_events) > 1:
                raise BetfairReplaceSagaIntegrityError("replace saga has multiple PROVIDER_RESULT events")
            if provider_events:
                event = provider_events[0]
                if set(event["payload"]) != {"evidence"}:
                    raise BetfairReplaceSagaIntegrityError("replace PROVIDER_RESULT schema invalid")
                evidence = _provider_evidence_from_dict(event["payload"]["evidence"])
                if _timestamp(event["recorded_at"], "recorded_at") != evidence.observed_at:
                    raise BetfairReplaceSagaIntegrityError(
                        "PROVIDER_RESULT recorded_at mismatches evidence observed_at"
                    )
                if not _same_id_set(intent, evidence.results, attr="bet_id"):
                    raise BetfairReplaceSagaIntegrityError("provider result does not cover exact prepared bet ids")
                if submitted_at is None or submitted_index is None:
                    raise BetfairReplaceSagaIntegrityError(
                        "provider result requires prior SUBMITTED boundary"
                    )
                if saga_events.index(event) <= submitted_index:
                    raise BetfairReplaceSagaIntegrityError(
                        "provider result must follow SUBMITTED in journal order"
                    )
                if _time(evidence.observed_at) < _time(submitted_at):
                    raise BetfairReplaceSagaIntegrityError(
                        "provider result precedes replace causal boundary"
                    )
            reconciliation_events = [e for e in saga_events if e["event_type"] == EventType.RECONCILIATION.value]
            if unknown_events:
                unknown_index = saga_events.index(unknown_events[0])
                if provider_events and unknown_index > saga_events.index(provider_events[0]):
                    raise BetfairReplaceSagaIntegrityError(
                        "replace UNKNOWN cannot follow durable provider result"
                    )
                if reconciliation_events and unknown_index > saga_events.index(reconciliation_events[0]):
                    raise BetfairReplaceSagaIntegrityError(
                        "replace UNKNOWN cannot follow durable reconciliation"
                    )
            seen_evidence: dict[str, dict[str, object]] = {}
            previous_reconciliation_at: str | None = None
            for event in reconciliation_events:
                if set(event["payload"]) != {"evidence"}:
                    raise BetfairReplaceSagaIntegrityError("replace RECONCILIATION schema invalid")
                evidence = _reconciliation_from_dict(event["payload"]["evidence"])
                if _timestamp(event["recorded_at"], "recorded_at") != evidence.observed_at:
                    raise BetfairReplaceSagaIntegrityError(
                        "RECONCILIATION recorded_at mismatches evidence observed_at"
                    )
                if not _same_id_set(intent, evidence.results, attr="bet_id"):
                    raise BetfairReplaceSagaIntegrityError("reconciliation does not cover exact prepared bet ids")
                if submitted_at is None or submitted_index is None:
                    raise BetfairReplaceSagaIntegrityError(
                        "reconciliation requires prior SUBMITTED boundary"
                    )
                if saga_events.index(event) <= submitted_index:
                    raise BetfairReplaceSagaIntegrityError(
                        "reconciliation must follow SUBMITTED in journal order"
                    )
                if _time(evidence.observed_at) <= _time(submitted_at):
                    raise BetfairReplaceSagaIntegrityError(
                        "reconciliation must be newer than replace external boundary"
                    )
                if (
                    previous_reconciliation_at is not None
                    and _time(evidence.observed_at) <= _time(previous_reconciliation_at)
                ):
                    raise BetfairReplaceSagaIntegrityError(
                        "reconciliation observed_at must strictly advance in journal order"
                    )
                previous_reconciliation_at = evidence.observed_at
                prior = seen_evidence.get(evidence.evidence_id)
                if prior is not None and prior != evidence.to_dict():
                    raise BetfairReplaceSagaIntegrityError(
                        "reconciliation evidence id has conflicting payloads"
                    )
                seen_evidence[evidence.evidence_id] = evidence.to_dict()

    def prepare(self, intent: ReplaceIntent) -> ReplaceSagaSnapshot:
        if type(intent) is not ReplaceIntent:
            raise TypeError("intent must be exact ReplaceIntent")
        with durable_path_lock(self._absolute_path()):
            events = self._events()
            existing = self._saga_events(events, intent.saga_id)
            if existing:
                stored = self._intent_from_events(events, intent.saga_id)
                if stored != intent:
                    raise BetfairReplaceSagaIdentityConflict(
                        "saga_id reused with different immutable replace intent"
                    )
                return self._snapshot_from_events(events, intent.saga_id)
            self._append(EventType.PREPARED, intent.saga_id, {"intent": intent.to_dict()}, intent.prepared_at)
            return self._snapshot_locked(intent.saga_id)

    def mark_submitted(self, saga_id: str, *, submitted_at: str) -> ReplaceSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        canonical_time = _timestamp(submitted_at, "submitted_at")
        with durable_path_lock(self._absolute_path()):
            events = self._events()
            intent = self._intent_from_events(events, saga)
            existing = [e for e in self._saga_events(events, saga) if e["event_type"] == EventType.SUBMITTED.value]
            payload = {"submitted_at": canonical_time, "request_sha256": intent.request_sha256}
            if existing:
                if existing[0]["payload"] == payload:
                    return self._snapshot_from_events(events, saga)
                raise BetfairReplaceSagaIdentityConflict(
                    "replace saga already has a different submission boundary"
                )
            facts = self._facts(events, saga)
            if facts.provider is not None or facts.reconciliation is not None:
                raise BetfairReplaceSagaStateError("cannot submit after provider/reconciliation evidence")
            if _time(canonical_time) < _time(intent.prepared_at):
                raise BetfairReplaceSagaStateError("submitted_at precedes prepared_at")
            self._append(EventType.SUBMITTED, saga, payload, canonical_time)
            return self._snapshot_locked(saga)

    def mark_unknown(self, saga_id: str, *, reason: str, observed_at: str) -> ReplaceSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        reason_text = _text(reason, "reason")
        canonical_time = _timestamp(observed_at, "observed_at")
        with durable_path_lock(self._absolute_path()):
            events = self._events()
            intent = self._intent_from_events(events, saga)
            facts = self._facts(events, saga)
            if facts.provider is not None or facts.reconciliation is not None:
                raise BetfairReplaceSagaStateError(
                    "provider/reconciliation evidence is already durable; ambiguity cannot replace it"
                )
            if facts.submitted_at is None:
                raise BetfairReplaceSagaStateError(
                    "UNKNOWN requires durable SUBMITTED boundary"
                )
            if _time(canonical_time) < _time(facts.submitted_at):
                raise BetfairReplaceSagaStateError(
                    "UNKNOWN precedes replace causal boundary"
                )
            payload = {"reason": reason_text, "observed_at": canonical_time}
            existing = [e for e in self._saga_events(events, saga) if e["event_type"] == EventType.UNKNOWN.value]
            if existing:
                if existing[0]["payload"] == payload:
                    return self._snapshot_from_events(events, saga)
                raise BetfairReplaceSagaIdentityConflict(
                    "replace saga already has different uncertainty evidence"
                )
            self._append(EventType.UNKNOWN, saga, payload, canonical_time)
            return self._snapshot_locked(saga)

    def record_provider_result(self, saga_id: str, evidence: ReplaceProviderEvidence) -> ReplaceSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        if type(evidence) is not ReplaceProviderEvidence:
            raise TypeError("evidence must be exact ReplaceProviderEvidence")
        with durable_path_lock(self._absolute_path()):
            events = self._events()
            intent = self._intent_from_events(events, saga)
            facts = self._facts(events, saga)
            if facts.submitted_at is None:
                raise BetfairReplaceSagaStateError(
                    "provider result requires durable SUBMITTED boundary"
                )
            if not _same_id_set(intent, evidence.results, attr="bet_id"):
                raise BetfairReplaceSagaIdentityConflict(
                    "provider result does not cover exact prepared bet ids"
                )
            if _time(evidence.observed_at) < _time(facts.submitted_at):
                raise BetfairReplaceSagaStateError("provider result precedes submission")
            existing = [e for e in self._saga_events(events, saga) if e["event_type"] == EventType.PROVIDER_RESULT.value]
            payload = {"evidence": evidence.to_dict()}
            if existing:
                if existing[0]["payload"] == payload:
                    return self._snapshot_from_events(events, saga)
                raise BetfairReplaceSagaIdentityConflict(
                    "replace saga already has different provider result evidence"
                )
            self._append(EventType.PROVIDER_RESULT, saga, payload, evidence.observed_at)
            return self._snapshot_locked(saga)

    def record_reconciliation(
        self,
        saga_id: str,
        evidence: ReplaceReconciliationEvidence,
    ) -> ReplaceSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        if type(evidence) is not ReplaceReconciliationEvidence:
            raise TypeError("evidence must be exact ReplaceReconciliationEvidence")
        with durable_path_lock(self._absolute_path()):
            events = self._events()
            intent = self._intent_from_events(events, saga)
            facts = self._facts(events, saga)
            if facts.submitted_at is None:
                raise BetfairReplaceSagaStateError(
                    "reconciliation requires durable SUBMITTED boundary"
                )
            if _time(evidence.observed_at) <= _time(facts.submitted_at):
                raise BetfairReplaceSagaStateError(
                    "reconciliation must be newer than replace external boundary"
                )
            if not _same_id_set(intent, evidence.results, attr="bet_id"):
                raise BetfairReplaceSagaIdentityConflict(
                    "reconciliation does not cover exact prepared bet ids"
                )
            for event in self._saga_events(events, saga):
                if event["event_type"] != EventType.RECONCILIATION.value:
                    continue
                prior = _reconciliation_from_dict(event["payload"]["evidence"])
                if prior.evidence_id == evidence.evidence_id:
                    if prior == evidence:
                        return self._snapshot_from_events(events, saga)
                    raise BetfairReplaceSagaIdentityConflict(
                        "reconciliation evidence id reused with different payload"
                    )
                if _time(prior.observed_at) >= _time(evidence.observed_at):
                    raise BetfairReplaceSagaStateError(
                        "reconciliation evidence must advance observed_at"
                    )
            self._append(
                EventType.RECONCILIATION,
                saga,
                {"evidence": evidence.to_dict()},
                evidence.observed_at,
            )
            return self._snapshot_locked(saga)

    def _snapshot_from_events(self, events: list[dict[str, object]], saga_id: str) -> ReplaceSagaSnapshot:
        intent = self._intent_from_events(events, saga_id)
        facts = self._facts(events, saga_id)
        state, exposure = _derive_state_and_exposure(intent, facts)
        raw = self.path.read_bytes() if self.path.exists() else b""
        return ReplaceSagaSnapshot(
            intent=intent,
            state=state,
            retry_disposition=_retry_disposition(state),
            exposure=exposure,
            event_count=len(self._saga_events(events, saga_id)),
            journal_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def _snapshot_locked(self, saga: str) -> ReplaceSagaSnapshot:
        events = self._events()
        if not self._saga_events(events, saga):
            raise KeyError(saga)
        return self._snapshot_from_events(events, saga)

    def snapshot(self, saga_id: str) -> ReplaceSagaSnapshot:
        saga = _text(saga_id, "saga_id")
        with durable_path_lock(self._absolute_path()):
            return self._snapshot_locked(saga)

    def verify_integrity(self) -> int:
        with durable_path_lock(self._absolute_path()):
            return len(self._events())
