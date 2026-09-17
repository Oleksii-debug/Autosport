from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class ExecutionLedgerError(RuntimeError):
    pass


class ExecutionLedgerIntegrityError(ExecutionLedgerError):
    pass


class ExecutionStateError(ExecutionLedgerError):
    pass


class ExecutionIdentityConflict(ExecutionLedgerError):
    pass


class ExecutionLedgerBusyError(ExecutionLedgerError):
    pass


class AcknowledgementStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"


class AttemptState(str, Enum):
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    RECONCILED_NOT_FOUND = "RECONCILED_NOT_FOUND"


class EventType(str, Enum):
    PLAN_RESERVED = "PLAN_RESERVED"
    ATTEMPT_RESERVED = "ATTEMPT_RESERVED"
    ATTEMPT_SUBMITTED = "ATTEMPT_SUBMITTED"
    ATTEMPT_UNKNOWN = "ATTEMPT_UNKNOWN"
    EXTERNAL_ACKNOWLEDGEMENT = "EXTERNAL_ACKNOWLEDGEMENT"
    RECONCILED_NOT_FOUND = "RECONCILED_NOT_FOUND"
    PLAN_STALE = "PLAN_STALE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    value.encode("utf-8")
    return value


def _decimal(value: Decimal | str | int, name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be finite and > 0")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionLedgerIntegrityError("value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionLedgerIntegrityError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ExecutionLedgerIntegrityError(f"non-finite JSON number {value!r}")


def _validate_json(value: Any, path: str = "event") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            value.encode("utf-8")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExecutionLedgerIntegrityError(f"non-finite JSON value at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExecutionLedgerIntegrityError(f"non-text JSON key at {path}")
            _validate_json(item, f"{path}.{key}")
        return
    raise ExecutionLedgerIntegrityError(
        f"unsupported JSON type {type(value).__name__} at {path}"
    )


@dataclass(frozen=True, slots=True)
class ExecutionAction:
    action_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    requested_odds: Decimal | str | int
    requested_stake: Decimal | str | int
    quote_id: str
    quote_observed_at: str
    expires_at: str

    def __post_init__(self) -> None:
        for name in (
            "action_id", "bookmaker_id", "account_id", "event_id", "market_id",
            "selection_id", "side", "quote_id", "quote_observed_at", "expires_at",
        ):
            _text(getattr(self, name), name)
        object.__setattr__(self, "requested_odds", _decimal(self.requested_odds, "requested_odds"))
        object.__setattr__(self, "requested_stake", _decimal(self.requested_stake, "requested_stake"))

    def to_dict(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "requested_odds": _decimal_text(self.requested_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "quote_id": self.quote_id,
            "quote_observed_at": self.quote_observed_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    bookmaker_profile_version: str
    decision_id: str
    approval_id: str
    created_at: str
    actions: tuple[ExecutionAction, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("plan_id", "bookmaker_profile_version", "decision_id", "approval_id", "created_at"):
            _text(getattr(self, name), name)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported execution plan schema")
        actions = tuple(self.actions)
        if not actions or len({a.action_id for a in actions}) != len(actions):
            raise ValueError("execution plan needs unique non-empty actions")
        object.__setattr__(self, "actions", actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "bookmaker_profile_version": self.bookmaker_profile_version,
            "decision_id": self.decision_id,
            "approval_id": self.approval_id,
            "created_at": self.created_at,
            "actions": [action.to_dict() for action in self.actions],
        }

    @property
    def fingerprint(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    attempt_id: str
    plan_id: str
    action_id: str
    effect_fingerprint: str
    reserved_at: str


@dataclass(frozen=True, slots=True)
class ExternalAcknowledgement:
    attempt_id: str
    external_receipt_id: str
    status: AcknowledgementStatus
    acknowledged_at: str
    accepted_odds: Decimal | str | int | None = None
    accepted_stake: Decimal | str | int | None = None
    reconciliation_evidence_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.attempt_id, "attempt_id")
        _text(self.external_receipt_id, "external_receipt_id")
        _text(self.acknowledged_at, "acknowledged_at")
        if self.reconciliation_evidence_id is not None:
            _text(self.reconciliation_evidence_id, "reconciliation_evidence_id")
        if self.status in {AcknowledgementStatus.ACCEPTED, AcknowledgementStatus.PARTIAL}:
            if self.accepted_odds is None or self.accepted_stake is None:
                raise ValueError("accepted/partial acknowledgement requires odds and stake")
            object.__setattr__(self, "accepted_odds", _decimal(self.accepted_odds, "accepted_odds"))
            object.__setattr__(self, "accepted_stake", _decimal(self.accepted_stake, "accepted_stake"))
        elif self.accepted_odds is not None or self.accepted_stake is not None:
            raise ValueError("rejected acknowledgement cannot claim accepted odds/stake")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "external_receipt_id": self.external_receipt_id,
            "status": self.status.value,
            "acknowledged_at": self.acknowledged_at,
            "accepted_odds": _decimal_text(self.accepted_odds) if self.accepted_odds is not None else None,
            "accepted_stake": _decimal_text(self.accepted_stake) if self.accepted_stake is not None else None,
            "reconciliation_evidence_id": self.reconciliation_evidence_id,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    attempt_id: str
    evidence_id: str
    observed_at: str
    external_effect_found: bool
    source: str

    def __post_init__(self) -> None:
        for name in ("attempt_id", "evidence_id", "observed_at", "source"):
            _text(getattr(self, name), name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at,
            "external_effect_found": self.external_effect_found,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ExecutionSaga:
    plan_id: str
    plan_fingerprint: str
    stale: bool
    attempts: dict[str, AttemptState]
    attempt_action_ids: dict[str, str]
    receipts: dict[str, str]


@dataclass(frozen=True, slots=True)
class VerifiedExecutionLedgerSnapshot:
    payload: bytes
    sha256: str
    event_count: int


class RealExecutionLedger:
    """Durable execution facts only; deliberately contains no provider write capability."""

    _FIELDS = frozenset(
        {"schema_version", "event_id", "event_type", "recorded_at", "plan_id",
         "action_id", "attempt_id", "payload"}
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".writer.lock")
        self._thread_lock = threading.RLock()

    def _mutate(self, operation):
        with self._thread_lock:
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                raise ExecutionLedgerBusyError(
                    "writer lock exists; fail closed until writer/crash ownership is resolved"
                ) from exc
            try:
                return operation()
            finally:
                os.close(fd)
                try:
                    self._lock_path.unlink()
                except FileNotFoundError:
                    pass

    @classmethod
    def _validate_event(cls, event: object, line: int | None = None) -> dict[str, Any]:
        where = f" at line {line}" if line else ""
        if not isinstance(event, dict) or set(event) != cls._FIELDS:
            raise ExecutionLedgerIntegrityError(f"execution event schema invalid{where}")
        if event["schema_version"] != SCHEMA_VERSION:
            raise ExecutionLedgerIntegrityError(f"unsupported event schema{where}")
        for name in ("event_id", "event_type", "recorded_at", "plan_id"):
            if not isinstance(event[name], str) or not event[name].strip():
                raise ExecutionLedgerIntegrityError(f"invalid {name}{where}")
        if event["event_type"] not in {item.value for item in EventType}:
            raise ExecutionLedgerIntegrityError(f"unknown event type{where}")
        for name in ("action_id", "attempt_id"):
            value = event[name]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ExecutionLedgerIntegrityError(f"invalid {name}{where}")
        if not isinstance(event["payload"], dict):
            raise ExecutionLedgerIntegrityError(f"invalid payload{where}")
        _validate_json(event)
        return event

    @classmethod
    def _parse(cls, raw: bytes) -> list[dict[str, Any]]:
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise ExecutionLedgerIntegrityError("execution ledger has unterminated final event")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionLedgerIntegrityError("execution ledger is not valid UTF-8") from exc
        events: list[dict[str, Any]] = []
        ids: set[str] = set()
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line:
                raise ExecutionLedgerIntegrityError(f"blank event at line {line_no}")
            try:
                envelope = json.loads(line, object_pairs_hook=_pairs, parse_constant=_nonfinite)
            except json.JSONDecodeError as exc:
                raise ExecutionLedgerIntegrityError(f"invalid JSON at line {line_no}") from exc
            if not isinstance(envelope, dict) or set(envelope) != {"sha256", "event"}:
                raise ExecutionLedgerIntegrityError(f"invalid envelope at line {line_no}")
            event = cls._validate_event(envelope["event"], line_no)
            if envelope["sha256"] != _digest(event):
                raise ExecutionLedgerIntegrityError(f"execution ledger SHA-256 mismatch at line {line_no}")
            if event["event_id"] in ids:
                raise ExecutionLedgerIntegrityError(f"duplicate event_id at line {line_no}")
            ids.add(event["event_id"])
            events.append(event)
        return events

    def _events(self) -> list[dict[str, Any]]:
        return self._parse(self.path.read_bytes()) if self.path.exists() else []

    def _append(self, kind: EventType, plan_id: str, action_id: str | None,
                attempt_id: str | None, payload: dict[str, Any]) -> None:
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "event_type": kind.value,
            "recorded_at": _now(),
            "plan_id": plan_id,
            "action_id": action_id,
            "attempt_id": attempt_id,
            "payload": payload,
        }
        self._validate_event(event)
        envelope = _canonical({"sha256": _digest(event), "event": event})
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(envelope + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _plan_event(events: list[dict[str, Any]], plan_id: str) -> dict[str, Any] | None:
        matches = [e for e in events if e["plan_id"] == plan_id and e["event_type"] == EventType.PLAN_RESERVED.value]
        if len(matches) > 1 and len({e["payload"]["plan_fingerprint"] for e in matches}) != 1:
            raise ExecutionLedgerIntegrityError(f"conflicting plan records for {plan_id}")
        return matches[0] if matches else None

    @staticmethod
    def _attempt_events(events: list[dict[str, Any]], attempt_id: str) -> list[dict[str, Any]]:
        return [e for e in events if e["attempt_id"] == attempt_id]

    @classmethod
    def _state(cls, events: list[dict[str, Any]]) -> AttemptState | None:
        state = None
        for event in events:
            kind = event["event_type"]
            if kind == EventType.ATTEMPT_RESERVED.value:
                if state is not None:
                    raise ExecutionLedgerIntegrityError("attempt reserved more than once")
                state = AttemptState.RESERVED
            elif kind == EventType.ATTEMPT_SUBMITTED.value:
                if state != AttemptState.RESERVED:
                    raise ExecutionLedgerIntegrityError("submission without reservation")
                state = AttemptState.SUBMITTED
            elif kind == EventType.ATTEMPT_UNKNOWN.value:
                if state not in {AttemptState.RESERVED, AttemptState.SUBMITTED}:
                    raise ExecutionLedgerIntegrityError("UNKNOWN from invalid state")
                state = AttemptState.UNKNOWN
            elif kind == EventType.EXTERNAL_ACKNOWLEDGEMENT.value:
                if state not in {AttemptState.RESERVED, AttemptState.SUBMITTED, AttemptState.UNKNOWN}:
                    raise ExecutionLedgerIntegrityError("acknowledgement from invalid state")
                state = AttemptState(event["payload"]["status"])
            elif kind == EventType.RECONCILED_NOT_FOUND.value:
                if state != AttemptState.UNKNOWN:
                    raise ExecutionLedgerIntegrityError("not-found reconciliation requires UNKNOWN")
                state = AttemptState.RECONCILED_NOT_FOUND
        return state

    @staticmethod
    def _stale(events: list[dict[str, Any]], plan_id: str) -> bool:
        return any(e["plan_id"] == plan_id and e["event_type"] == EventType.PLAN_STALE.value for e in events)

    @classmethod
    def _receipt_owners(cls, events: list[dict[str, Any]]) -> dict[str, str]:
        owners: dict[str, str] = {}
        for event in events:
            if event["event_type"] != EventType.EXTERNAL_ACKNOWLEDGEMENT.value:
                continue
            receipt, attempt = event["payload"]["external_receipt_id"], event["attempt_id"]
            if receipt in owners and owners[receipt] != attempt:
                raise ExecutionLedgerIntegrityError("external receipt belongs to multiple attempts")
            owners[receipt] = attempt
        return owners

    def _action(self, events: list[dict[str, Any]], plan_id: str, action_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        plan_event = self._plan_event(events, plan_id)
        if plan_event is None:
            raise ExecutionStateError(f"plan {plan_id!r} is not reserved")
        matches = [a for a in plan_event["payload"]["plan"]["actions"] if a["action_id"] == action_id]
        if len(matches) != 1:
            raise ExecutionStateError(f"action {action_id!r} does not belong to plan")
        return plan_event, matches[0]

    def reserve_plan(self, plan: ExecutionPlan) -> str:
        def operation() -> str:
            events = self._events()
            prior = self._plan_event(events, plan.plan_id)
            if prior:
                if prior["payload"]["plan_fingerprint"] != plan.fingerprint:
                    raise ExecutionIdentityConflict("plan_id reused with different immutable inputs")
                return plan.fingerprint
            self._append(EventType.PLAN_RESERVED, plan.plan_id, None, None,
                         {"plan_fingerprint": plan.fingerprint, "plan": plan.to_dict()})
            return plan.fingerprint
        return self._mutate(operation)

    def begin_attempt(self, *, plan_id: str, action_id: str, attempt_id: str,
                      reserved_at: str | None = None) -> ExecutionAttempt:
        _text(attempt_id, "attempt_id")
        reserved_at = reserved_at or _now()

        def operation() -> ExecutionAttempt:
            events = self._events()
            plan_event, action = self._action(events, plan_id, action_id)
            if self._stale(events, plan_id):
                raise ExecutionStateError("execution plan is stale; recompute before another action")
            fingerprint = _digest({
                "plan_fingerprint": plan_event["payload"]["plan_fingerprint"],
                "action": action,
            })
            prior_events = self._attempt_events(events, attempt_id)
            if prior_events:
                prior = prior_events[0]
                if (prior["plan_id"], prior["action_id"], prior["payload"].get("effect_fingerprint")) != (plan_id, action_id, fingerprint):
                    raise ExecutionIdentityConflict("attempt_id reused with different immutable inputs")
                return ExecutionAttempt(attempt_id, plan_id, action_id, fingerprint, prior["payload"]["reserved_at"])
            for event in events:
                if event["plan_id"] == plan_id and event["action_id"] == action_id and event["event_type"] == EventType.ATTEMPT_RESERVED.value:
                    if self._state(self._attempt_events(events, event["attempt_id"])) != AttemptState.RECONCILED_NOT_FOUND:
                        raise ExecutionStateError("action already has unresolved/final attempt")
            self._append(EventType.ATTEMPT_RESERVED, plan_id, action_id, attempt_id,
                         {"effect_fingerprint": fingerprint, "reserved_at": reserved_at})
            return ExecutionAttempt(attempt_id, plan_id, action_id, fingerprint, reserved_at)
        return self._mutate(operation)

    def mark_submitted(self, attempt_id: str, submitted_at: str | None = None) -> None:
        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(events, attempt_id)
            state = self._state(attempt_events)
            if state == AttemptState.SUBMITTED:
                return
            if state != AttemptState.RESERVED:
                raise ExecutionStateError("only reserved attempt can be submitted")
            first = attempt_events[0]
            self._append(EventType.ATTEMPT_SUBMITTED, first["plan_id"], first["action_id"],
                         attempt_id, {"submitted_at": submitted_at or _now()})
        self._mutate(operation)

    def mark_unknown(self, attempt_id: str, *, reason: str, observed_at: str | None = None) -> None:
        _text(reason, "reason")
        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(events, attempt_id)
            state = self._state(attempt_events)
            if state == AttemptState.UNKNOWN:
                return
            if state not in {AttemptState.RESERVED, AttemptState.SUBMITTED}:
                raise ExecutionStateError("only unresolved attempt can become UNKNOWN")
            first = attempt_events[0]
            self._append(EventType.ATTEMPT_UNKNOWN, first["plan_id"], first["action_id"],
                         attempt_id, {"reason": reason, "observed_at": observed_at or _now()})
        self._mutate(operation)

    def recover_uncertain(self, *, reason: str = "process_restart") -> tuple[str, ...]:
        def operation() -> tuple[str, ...]:
            promoted: list[str] = []
            events = self._events()
            for reserved in [e for e in events if e["event_type"] == EventType.ATTEMPT_RESERVED.value]:
                attempt_id = reserved["attempt_id"]
                if self._state(self._attempt_events(events, attempt_id)) in {AttemptState.RESERVED, AttemptState.SUBMITTED}:
                    self._append(EventType.ATTEMPT_UNKNOWN, reserved["plan_id"], reserved["action_id"],
                                 attempt_id, {"reason": reason, "observed_at": _now()})
                    promoted.append(attempt_id)
                    events = self._events()
            return tuple(promoted)
        return self._mutate(operation)

    def acknowledge(self, acknowledgement: ExternalAcknowledgement) -> None:
        payload = acknowledgement.to_dict()
        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(events, acknowledgement.attempt_id)
            existing = [e for e in attempt_events if e["event_type"] == EventType.EXTERNAL_ACKNOWLEDGEMENT.value]
            if existing:
                if existing[0]["payload"] == payload:
                    return
                raise ExecutionIdentityConflict("attempt already has a different acknowledgement")
            if self._state(attempt_events) not in {AttemptState.RESERVED, AttemptState.SUBMITTED, AttemptState.UNKNOWN}:
                raise ExecutionStateError("acknowledgement requires unresolved attempt")
            prior = self._receipt_owners(events).get(acknowledgement.external_receipt_id)
            if prior is not None and prior != acknowledgement.attempt_id:
                raise ExecutionIdentityConflict("external receipt already belongs to another attempt")
            first = attempt_events[0]
            self._append(EventType.EXTERNAL_ACKNOWLEDGEMENT, first["plan_id"], first["action_id"],
                         acknowledgement.attempt_id, payload)
            plan_event = self._plan_event(events, first["plan_id"])
            if len(plan_event["payload"]["plan"]["actions"]) > 1 and not self._stale(events, first["plan_id"]):
                self._append(EventType.PLAN_STALE, first["plan_id"], first["action_id"],
                             acknowledgement.attempt_id,
                             {"reason": "ack_requires_portfolio_recompute", "status": acknowledgement.status.value})
        self._mutate(operation)

    def reconcile_not_found(self, snapshot: ReconciliationSnapshot) -> None:
        if snapshot.external_effect_found:
            raise ValueError("found external effect must be reconciled as acknowledgement")
        payload = snapshot.to_dict()
        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(events, snapshot.attempt_id)
            existing = [e for e in attempt_events if e["event_type"] == EventType.RECONCILED_NOT_FOUND.value]
            if existing:
                if existing[0]["payload"] == payload:
                    return
                raise ExecutionIdentityConflict("different reconciliation evidence already exists")
            if self._state(attempt_events) != AttemptState.UNKNOWN:
                raise ExecutionStateError("retry requires UNKNOWN + external not-found evidence")
            first = attempt_events[0]
            self._append(EventType.RECONCILED_NOT_FOUND, first["plan_id"], first["action_id"],
                         snapshot.attempt_id, payload)
        self._mutate(operation)

    def verified_snapshot(self) -> VerifiedExecutionLedgerSnapshot:
        raw = self.path.read_bytes() if self.path.exists() else b""
        events = self._parse(raw)
        return VerifiedExecutionLedgerSnapshot(raw, hashlib.sha256(raw).hexdigest(), len(events))

    def verify_integrity(self) -> int:
        return self.verified_snapshot().event_count

    def attempt_state(self, attempt_id: str) -> AttemptState:
        state = self._state(self._attempt_events(self._events(), attempt_id))
        if state is None:
            raise KeyError(attempt_id)
        return state

    def plan_is_stale(self, plan_id: str) -> bool:
        events = self._events()
        if self._plan_event(events, plan_id) is None:
            raise KeyError(plan_id)
        return self._stale(events, plan_id)

    def can_retry_action(self, *, plan_id: str, action_id: str) -> bool:
        events = self._events()
        self._action(events, plan_id, action_id)
        if self._stale(events, plan_id):
            return False
        attempts = [e["attempt_id"] for e in events
                    if e["plan_id"] == plan_id and e["action_id"] == action_id
                    and e["event_type"] == EventType.ATTEMPT_RESERVED.value]
        return not attempts or self._state(self._attempt_events(events, attempts[-1])) == AttemptState.RECONCILED_NOT_FOUND

    def saga(self, plan_id: str) -> ExecutionSaga:
        events = self._events()
        plan_event = self._plan_event(events, plan_id)
        if plan_event is None:
            raise KeyError(plan_id)
        attempts: dict[str, AttemptState] = {}
        action_ids: dict[str, str] = {}
        for event in events:
            if event["plan_id"] == plan_id and event["event_type"] == EventType.ATTEMPT_RESERVED.value:
                attempt_id = event["attempt_id"]
                state = self._state(self._attempt_events(events, attempt_id))
                attempts[attempt_id] = state
                action_ids[attempt_id] = event["action_id"]
        receipts = {r: a for r, a in self._receipt_owners(events).items() if a in attempts}
        return ExecutionSaga(plan_id, plan_event["payload"]["plan_fingerprint"],
                             self._stale(events, plan_id), attempts, action_ids, receipts)
