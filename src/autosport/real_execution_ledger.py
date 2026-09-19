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
from typing import Any, Callable, TypeVar


SCHEMA_VERSION = 1
_T = TypeVar("_T")


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
    SUPERVISED_APPROVAL_BOUND = "SUPERVISED_APPROVAL_BOUND"
    SUPERVISED_APPROVAL_REVOKED = "SUPERVISED_APPROVAL_REVOKED"
    ATTEMPT_RESERVED = "ATTEMPT_RESERVED"
    ATTEMPT_SUBMITTED = "ATTEMPT_SUBMITTED"
    ATTEMPT_UNKNOWN = "ATTEMPT_UNKNOWN"
    PROVIDER_ORDER_REFERENCE_BOUND = "PROVIDER_ORDER_REFERENCE_BOUND"
    PROVIDER_EVIDENCE_BOUND = "PROVIDER_EVIDENCE_BOUND"
    EXTERNAL_ACKNOWLEDGEMENT = "EXTERNAL_ACKNOWLEDGEMENT"
    RECONCILED_FOUND = "RECONCILED_FOUND"
    RECONCILED_NOT_FOUND = "RECONCILED_NOT_FOUND"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256_text(value: str, name: str) -> str:
    _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _timestamp(value: str, name: str) -> datetime:
    _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


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
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
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
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ExecutionLedgerIntegrityError(f"invalid UTF-8 text at {path}") from exc
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
            _validate_json(key, f"{path} object key")
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
            "action_id",
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "quote_id",
        ):
            _text(getattr(self, name), name)
        observed = _timestamp(self.quote_observed_at, "quote_observed_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= observed:
            raise ValueError("expires_at must be after quote_observed_at")
        object.__setattr__(
            self, "requested_odds", _decimal(self.requested_odds, "requested_odds")
        )
        object.__setattr__(
            self, "requested_stake", _decimal(self.requested_stake, "requested_stake")
        )

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
        for name in (
            "plan_id",
            "bookmaker_profile_version",
            "decision_id",
            "approval_id",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.created_at, "created_at")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported execution plan schema")
        actions = tuple(self.actions)
        if not actions or not all(isinstance(item, ExecutionAction) for item in actions):
            raise ValueError("execution plan requires ExecutionAction items")
        if len({action.action_id for action in actions}) != len(actions):
            raise ValueError("execution plan needs unique action_id values")
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
class ExternalReceiptIdentity:
    bookmaker_id: str
    account_id: str
    external_receipt_id: str


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
        _timestamp(self.acknowledged_at, "acknowledged_at")
        if not isinstance(self.status, AcknowledgementStatus):
            raise ValueError("status must be AcknowledgementStatus")
        if self.reconciliation_evidence_id is not None:
            _text(self.reconciliation_evidence_id, "reconciliation_evidence_id")
        if self.status in {
            AcknowledgementStatus.ACCEPTED,
            AcknowledgementStatus.PARTIAL,
        }:
            if self.accepted_odds is None or self.accepted_stake is None:
                raise ValueError(
                    "accepted/partial acknowledgement requires odds and stake"
                )
            object.__setattr__(
                self, "accepted_odds", _decimal(self.accepted_odds, "accepted_odds")
            )
            object.__setattr__(
                self, "accepted_stake", _decimal(self.accepted_stake, "accepted_stake")
            )
        elif self.accepted_odds is not None or self.accepted_stake is not None:
            raise ValueError(
                "rejected acknowledgement cannot claim accepted odds/stake"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "external_receipt_id": self.external_receipt_id,
            "status": self.status.value,
            "acknowledged_at": self.acknowledged_at,
            "accepted_odds": (
                _decimal_text(self.accepted_odds)
                if self.accepted_odds is not None
                else None
            ),
            "accepted_stake": (
                _decimal_text(self.accepted_stake)
                if self.accepted_stake is not None
                else None
            ),
            "reconciliation_evidence_id": self.reconciliation_evidence_id,
        }


@dataclass(frozen=True, slots=True)
class ExternalEffectReconciliation:
    attempt_id: str
    evidence_id: str
    external_receipt_id: str
    observed_at: str
    source: str

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "evidence_id",
            "external_receipt_id",
            "source",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.observed_at, "observed_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "evidence_id": self.evidence_id,
            "external_receipt_id": self.external_receipt_id,
            "observed_at": self.observed_at,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    attempt_id: str
    evidence_id: str
    observed_at: str
    external_effect_found: bool
    source: str

    def __post_init__(self) -> None:
        for name in ("attempt_id", "evidence_id", "source"):
            _text(getattr(self, name), name)
        _timestamp(self.observed_at, "observed_at")
        if type(self.external_effect_found) is not bool:
            raise ValueError("external_effect_found must be bool")

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
    receipts: dict[ExternalReceiptIdentity, str]


@dataclass(frozen=True, slots=True)
class VerifiedExecutionLedgerSnapshot:
    payload: bytes
    sha256: str
    event_count: int


class RealExecutionLedger:
    """Durable execution facts only; deliberately contains no provider write capability."""

    _FIELDS = frozenset(
        {
            "schema_version",
            "event_id",
            "event_type",
            "recorded_at",
            "plan_id",
            "action_id",
            "attempt_id",
            "payload",
        }
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".writer.lock")
        self._thread_lock = threading.RLock()
        # False until this instance has proven both the visible file contents and,
        # on POSIX, the directory entry naming the ledger durable.
        self._path_durable = False

    def _sync_parent_directory(self) -> None:
        """Durably publish this ledger pathname on platforms that require it."""

        if os.name == "nt":
            # Python maps os.fsync() to the Microsoft CRT _commit() on Windows.
            # The append path always fsyncs the just-created file before reaching
            # this hook, and Windows has no POSIX directory-fd fsync contract.
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(self.path.parent, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _ensure_existing_path_durable(self) -> None:
        """Re-establish a failed/unknown publish barrier before trusting events."""

        if self._path_durable or not self.path.exists():
            return
        try:
            # Re-fsync the visible file before publishing its directory entry.
            # Opening in append mode does not change a valid existing ledger.
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_parent_directory()
        except OSError as exc:
            self._path_durable = False
            raise ExecutionLedgerIntegrityError(
                "execution ledger durability barrier failed"
            ) from exc
        self._path_durable = True

    def _mutate(self, operation: Callable[[], _T]) -> _T:
        with self._thread_lock:
            try:
                fd = os.open(
                    self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
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
    def _validate_event(
        cls, event: object, line: int | None = None
    ) -> dict[str, Any]:
        where = f" at line {line}" if line else ""
        if not isinstance(event, dict) or set(event) != cls._FIELDS:
            raise ExecutionLedgerIntegrityError(
                f"execution event schema invalid{where}"
            )
        if type(event["schema_version"]) is not int or event["schema_version"] != SCHEMA_VERSION:
            raise ExecutionLedgerIntegrityError(f"unsupported event schema{where}")
        for name in ("event_id", "event_type", "recorded_at", "plan_id"):
            if not isinstance(event[name], str) or not event[name].strip():
                raise ExecutionLedgerIntegrityError(f"invalid {name}{where}")
        try:
            _timestamp(event["recorded_at"], "recorded_at")
        except ValueError as exc:
            raise ExecutionLedgerIntegrityError(
                f"invalid recorded_at{where}"
            ) from exc
        if event["event_type"] not in {item.value for item in EventType}:
            raise ExecutionLedgerIntegrityError(f"unknown event type{where}")
        for name in ("action_id", "attempt_id"):
            value = event[name]
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ExecutionLedgerIntegrityError(f"invalid {name}{where}")
        if not isinstance(event["payload"], dict):
            raise ExecutionLedgerIntegrityError(f"invalid payload{where}")
        try:
            _validate_json(event)
        except (UnicodeEncodeError, RecursionError) as exc:
            raise ExecutionLedgerIntegrityError(
                f"invalid execution event value{where}"
            ) from exc
        return event

    @classmethod
    def _parse(cls, raw: bytes) -> list[dict[str, Any]]:
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise ExecutionLedgerIntegrityError(
                "execution ledger has unterminated final event"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionLedgerIntegrityError(
                "execution ledger is not valid UTF-8"
            ) from exc
        events: list[dict[str, Any]] = []
        ids: set[str] = set()
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line:
                raise ExecutionLedgerIntegrityError(f"blank event at line {line_no}")
            try:
                envelope = json.loads(
                    line,
                    object_pairs_hook=_pairs,
                    parse_constant=_nonfinite,
                )
            except json.JSONDecodeError as exc:
                raise ExecutionLedgerIntegrityError(
                    f"invalid JSON at line {line_no}"
                ) from exc
            except RecursionError as exc:
                raise ExecutionLedgerIntegrityError(
                    f"JSON nesting is too deep at line {line_no}"
                ) from exc
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"sha256", "event"}
            ):
                raise ExecutionLedgerIntegrityError(
                    f"invalid envelope at line {line_no}"
                )
            digest = envelope["sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ExecutionLedgerIntegrityError(
                    f"invalid SHA-256 at line {line_no}"
                )
            event = cls._validate_event(envelope["event"], line_no)
            if digest != _digest(event):
                raise ExecutionLedgerIntegrityError(
                    f"execution ledger SHA-256 mismatch at line {line_no}"
                )
            if event["event_id"] in ids:
                raise ExecutionLedgerIntegrityError(
                    f"duplicate event_id at line {line_no}"
                )
            ids.add(event["event_id"])
            events.append(event)
        cls._validate_semantics(events)
        return events

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        self._ensure_existing_path_durable()
        return self._parse(self.path.read_bytes())

    def _append(
        self,
        kind: EventType,
        plan_id: str,
        action_id: str | None,
        attempt_id: str | None,
        payload: dict[str, Any],
    ) -> None:
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
        path_existed_before = self.path.exists()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(envelope + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not path_existed_before or not self._path_durable:
                self._sync_parent_directory()
        except OSError as exc:
            self._path_durable = False
            raise ExecutionLedgerIntegrityError(
                "execution ledger durability barrier failed"
            ) from exc
        self._path_durable = True

    @staticmethod
    def _plan_event(
        events: list[dict[str, Any]], plan_id: str
    ) -> dict[str, Any] | None:
        matches = [
            event
            for event in events
            if event["plan_id"] == plan_id
            and event["event_type"] == EventType.PLAN_RESERVED.value
        ]
        if len(matches) > 1:
            fingerprints = {
                event["payload"].get("plan_fingerprint") for event in matches
            }
            if len(fingerprints) != 1:
                raise ExecutionLedgerIntegrityError(
                    f"conflicting plan records for {plan_id}"
                )
        return matches[0] if matches else None

    @staticmethod
    def _attempt_events(
        events: list[dict[str, Any]], attempt_id: str
    ) -> list[dict[str, Any]]:
        return [event for event in events if event["attempt_id"] == attempt_id]

    @classmethod
    def _state(cls, events: list[dict[str, Any]]) -> AttemptState | None:
        state = None
        found_reconciliations: dict[str, dict[str, Any]] = {}
        found_receipt_id: str | None = None
        for event in events:
            kind = event["event_type"]
            if kind == EventType.ATTEMPT_RESERVED.value:
                if state is not None:
                    raise ExecutionLedgerIntegrityError(
                        "attempt reserved more than once"
                    )
                state = AttemptState.RESERVED
            elif kind == EventType.ATTEMPT_SUBMITTED.value:
                if state != AttemptState.RESERVED:
                    raise ExecutionLedgerIntegrityError(
                        "submission without reservation"
                    )
                state = AttemptState.SUBMITTED
            elif kind == EventType.ATTEMPT_UNKNOWN.value:
                if state not in {AttemptState.RESERVED, AttemptState.SUBMITTED}:
                    raise ExecutionLedgerIntegrityError(
                        "UNKNOWN from invalid state"
                    )
                state = AttemptState.UNKNOWN
            elif kind == EventType.RECONCILED_FOUND.value:
                if state != AttemptState.UNKNOWN:
                    raise ExecutionLedgerIntegrityError(
                        "found reconciliation requires UNKNOWN"
                    )
                evidence_id = event["payload"].get("evidence_id")
                if not isinstance(evidence_id, str) or not evidence_id.strip():
                    raise ExecutionLedgerIntegrityError(
                        "found reconciliation lacks evidence identity"
                    )
                prior = found_reconciliations.get(evidence_id)
                if prior is not None and prior != event["payload"]:
                    raise ExecutionLedgerIntegrityError(
                        "conflicting found reconciliation evidence"
                    )
                external_receipt_id = event["payload"].get("external_receipt_id")
                if (
                    not isinstance(external_receipt_id, str)
                    or not external_receipt_id.strip()
                ):
                    raise ExecutionLedgerIntegrityError(
                        "found reconciliation lacks receipt identity"
                    )
                if (
                    found_receipt_id is not None
                    and found_receipt_id != external_receipt_id
                ):
                    raise ExecutionLedgerIntegrityError(
                        "conflicting found reconciliation receipt identity"
                    )
                found_receipt_id = external_receipt_id
                found_reconciliations[evidence_id] = event["payload"]
            elif kind == EventType.PROVIDER_ORDER_REFERENCE_BOUND.value:
                if state != AttemptState.RESERVED:
                    raise ExecutionLedgerIntegrityError(
                        "provider order reference requires reserved attempt"
                    )
            elif kind == EventType.PROVIDER_EVIDENCE_BOUND.value:
                if state not in {AttemptState.SUBMITTED, AttemptState.UNKNOWN}:
                    raise ExecutionLedgerIntegrityError(
                        "provider evidence requires submitted/UNKNOWN attempt"
                    )
            elif kind == EventType.EXTERNAL_ACKNOWLEDGEMENT.value:
                if state not in {
                    AttemptState.SUBMITTED,
                    AttemptState.UNKNOWN,
                }:
                    raise ExecutionLedgerIntegrityError(
                        "acknowledgement from invalid state"
                    )
                evidence_id = event["payload"].get("reconciliation_evidence_id")
                if state == AttemptState.UNKNOWN:
                    if not evidence_id:
                        raise ExecutionLedgerIntegrityError(
                            "UNKNOWN acknowledgement lacks reconciliation evidence"
                        )
                    evidence = found_reconciliations.get(evidence_id)
                    if evidence is None:
                        raise ExecutionLedgerIntegrityError(
                            "UNKNOWN acknowledgement references missing reconciliation evidence"
                        )
                    if (
                        evidence.get("external_receipt_id")
                        != event["payload"].get("external_receipt_id")
                    ):
                        raise ExecutionLedgerIntegrityError(
                            "UNKNOWN acknowledgement receipt mismatches reconciliation evidence"
                        )
                elif evidence_id:
                    raise ExecutionLedgerIntegrityError(
                        "reconciliation evidence is only valid for UNKNOWN acknowledgement"
                    )
                try:
                    state = AttemptState(event["payload"]["status"])
                except (KeyError, ValueError) as exc:
                    raise ExecutionLedgerIntegrityError(
                        "acknowledgement has invalid status"
                    ) from exc
            elif kind == EventType.RECONCILED_NOT_FOUND.value:
                if state != AttemptState.UNKNOWN:
                    raise ExecutionLedgerIntegrityError(
                        "not-found reconciliation requires UNKNOWN"
                    )
                if found_reconciliations:
                    raise ExecutionLedgerIntegrityError(
                        "not-found reconciliation conflicts with positive evidence"
                    )
                state = AttemptState.RECONCILED_NOT_FOUND
        return state

    @classmethod
    def _action_payload(
        cls,
        events: list[dict[str, Any]],
        plan_id: str,
        action_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        plan_event = cls._plan_event(events, plan_id)
        if plan_event is None:
            raise ExecutionStateError(f"plan {plan_id!r} is not reserved")
        try:
            actions = plan_event["payload"]["plan"]["actions"]
        except (KeyError, TypeError) as exc:
            raise ExecutionLedgerIntegrityError("stored plan payload is invalid") from exc
        matches = [
            action for action in actions if action.get("action_id") == action_id
        ]
        if len(matches) != 1:
            raise ExecutionStateError(
                f"action {action_id!r} does not belong to plan"
            )
        return plan_event, matches[0]

    @classmethod
    def _receipt_identity(
        cls,
        events: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> ExternalReceiptIdentity:
        _, action = cls._action_payload(
            events, event["plan_id"], event["action_id"]
        )
        return ExternalReceiptIdentity(
            bookmaker_id=action["bookmaker_id"],
            account_id=action["account_id"],
            external_receipt_id=event["payload"]["external_receipt_id"],
        )

    @classmethod
    def _receipt_owners(
        cls, events: list[dict[str, Any]]
    ) -> dict[ExternalReceiptIdentity, str]:
        owners: dict[ExternalReceiptIdentity, str] = {}
        for event in events:
            if event["event_type"] not in {
                EventType.RECONCILED_FOUND.value,
                EventType.EXTERNAL_ACKNOWLEDGEMENT.value,
            }:
                continue
            identity = cls._receipt_identity(events, event)
            attempt = event["attempt_id"]
            if identity in owners and owners[identity] != attempt:
                raise ExecutionLedgerIntegrityError(
                    "provider/account external receipt belongs to multiple attempts"
                )
            owners[identity] = attempt
        return owners

    @classmethod
    def _stale(cls, events: list[dict[str, Any]], plan_id: str) -> bool:
        plan_event = cls._plan_event(events, plan_id)
        if plan_event is None:
            return False
        actions = plan_event["payload"]["plan"]["actions"]
        if len(actions) <= 1:
            return False
        # The ACK event itself is the crash-atomic stale authority. There is no
        # second fsync whose loss could reopen the old remaining plan.
        return any(
            event["plan_id"] == plan_id
            and event["event_type"]
            == EventType.EXTERNAL_ACKNOWLEDGEMENT.value
            for event in events
        )

    @classmethod
    def _plan_from_dict(cls, value: object) -> ExecutionPlan:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "plan_id",
            "bookmaker_profile_version",
            "decision_id",
            "approval_id",
            "created_at",
            "actions",
        }:
            raise ExecutionLedgerIntegrityError("stored plan schema is invalid")
        if not isinstance(value["actions"], list):
            raise ExecutionLedgerIntegrityError("stored plan actions are invalid")
        actions: list[ExecutionAction] = []
        expected_action_fields = {
            "action_id",
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "requested_odds",
            "requested_stake",
            "quote_id",
            "quote_observed_at",
            "expires_at",
        }
        try:
            for raw in value["actions"]:
                if not isinstance(raw, dict) or set(raw) != expected_action_fields:
                    raise ExecutionLedgerIntegrityError(
                        "stored action schema is invalid"
                    )
                actions.append(ExecutionAction(**raw))
            return ExecutionPlan(
                plan_id=value["plan_id"],
                bookmaker_profile_version=value["bookmaker_profile_version"],
                decision_id=value["decision_id"],
                approval_id=value["approval_id"],
                created_at=value["created_at"],
                actions=tuple(actions),
                schema_version=value["schema_version"],
            )
        except (TypeError, ValueError) as exc:
            raise ExecutionLedgerIntegrityError(
                "stored plan values are invalid"
            ) from exc

    @staticmethod
    def _acknowledgement_from_dict(value: object) -> ExternalAcknowledgement:
        expected_fields = {
            "attempt_id",
            "external_receipt_id",
            "status",
            "acknowledged_at",
            "accepted_odds",
            "accepted_stake",
            "reconciliation_evidence_id",
        }
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise ExecutionLedgerIntegrityError(
                "stored acknowledgement schema is invalid"
            )
        try:
            acknowledgement = ExternalAcknowledgement(
                attempt_id=value["attempt_id"],
                external_receipt_id=value["external_receipt_id"],
                status=AcknowledgementStatus(value["status"]),
                acknowledged_at=value["acknowledged_at"],
                accepted_odds=value["accepted_odds"],
                accepted_stake=value["accepted_stake"],
                reconciliation_evidence_id=value["reconciliation_evidence_id"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionLedgerIntegrityError(
                "stored acknowledgement values are invalid"
            ) from exc
        if acknowledgement.to_dict() != value:
            raise ExecutionLedgerIntegrityError(
                "stored acknowledgement payload is not canonical"
            )
        return acknowledgement

    @staticmethod
    def _found_reconciliation_from_dict(
        value: object,
    ) -> ExternalEffectReconciliation:
        expected_fields = {
            "attempt_id",
            "evidence_id",
            "external_receipt_id",
            "observed_at",
            "source",
        }
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise ExecutionLedgerIntegrityError(
                "stored found reconciliation schema is invalid"
            )
        try:
            reconciliation = ExternalEffectReconciliation(**value)
        except (TypeError, ValueError) as exc:
            raise ExecutionLedgerIntegrityError(
                "stored found reconciliation values are invalid"
            ) from exc
        if reconciliation.to_dict() != value:
            raise ExecutionLedgerIntegrityError(
                "stored found reconciliation payload is not canonical"
            )
        return reconciliation

    @staticmethod
    def _reconciliation_snapshot_from_dict(
        value: object,
    ) -> ReconciliationSnapshot:
        expected_fields = {
            "attempt_id",
            "evidence_id",
            "observed_at",
            "external_effect_found",
            "source",
        }
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise ExecutionLedgerIntegrityError(
                "stored not-found reconciliation schema is invalid"
            )
        try:
            snapshot = ReconciliationSnapshot(**value)
        except (TypeError, ValueError) as exc:
            raise ExecutionLedgerIntegrityError(
                "stored not-found reconciliation values are invalid"
            ) from exc
        if snapshot.to_dict() != value:
            raise ExecutionLedgerIntegrityError(
                "stored not-found reconciliation payload is not canonical"
            )
        return snapshot

    @classmethod
    def _validate_semantics(cls, events: list[dict[str, Any]]) -> None:
        plan_ids: set[str] = set()
        for event in events:
            if event["event_type"] != EventType.PLAN_RESERVED.value:
                continue
            if set(event["payload"]) != {"plan_fingerprint", "plan"}:
                raise ExecutionLedgerIntegrityError(
                    "PLAN_RESERVED payload schema is invalid"
                )
            plan = cls._plan_from_dict(event["payload"]["plan"])
            if event["plan_id"] != plan.plan_id:
                raise ExecutionLedgerIntegrityError(
                    "plan event identity does not match stored plan"
                )
            if event["action_id"] is not None or event["attempt_id"] is not None:
                raise ExecutionLedgerIntegrityError(
                    "PLAN_RESERVED cannot claim action/attempt identity"
                )
            if event["payload"]["plan_fingerprint"] != plan.fingerprint:
                raise ExecutionLedgerIntegrityError(
                    "stored plan fingerprint mismatch"
                )
            plan_ids.add(plan.plan_id)

        approval_state: dict[str, tuple[str, str, datetime, bool]] = {}
        for event in events:
            kind = event["event_type"]
            if kind not in {
                EventType.SUPERVISED_APPROVAL_BOUND.value,
                EventType.SUPERVISED_APPROVAL_REVOKED.value,
            }:
                continue
            if event["action_id"] is not None or event["attempt_id"] is not None:
                raise ExecutionLedgerIntegrityError(
                    "supervised approval event cannot claim action/attempt identity"
                )
            plan_event = cls._plan_event(events, event["plan_id"])
            if plan_event is None:
                raise ExecutionLedgerIntegrityError(
                    "supervised approval event references missing plan"
                )
            payload = event["payload"]
            if kind == EventType.SUPERVISED_APPROVAL_BOUND.value:
                if set(payload) != {
                    "approval_id",
                    "approval_fingerprint",
                    "approved_at",
                    "evidence_sha256",
                }:
                    raise ExecutionLedgerIntegrityError(
                        "supervised approval binding schema is invalid"
                    )
                try:
                    _sha256_text(payload["approval_fingerprint"], "approval_fingerprint")
                    _sha256_text(payload["evidence_sha256"], "evidence_sha256")
                    approved_at = _timestamp(payload["approved_at"], "approved_at")
                except (KeyError, TypeError, ValueError) as exc:
                    raise ExecutionLedgerIntegrityError(
                        "supervised approval binding values are invalid"
                    ) from exc
                if payload["approval_id"] != plan_event["payload"]["plan"]["approval_id"]:
                    raise ExecutionLedgerIntegrityError(
                        "supervised approval identity mismatches stored plan"
                    )
                prior = approval_state.get(event["plan_id"])
                current = (
                    payload["approval_id"],
                    payload["approval_fingerprint"],
                    approved_at,
                    False,
                )
                if prior is not None and prior != current:
                    raise ExecutionLedgerIntegrityError(
                        "conflicting supervised approval binding"
                    )
                approval_state[event["plan_id"]] = current
            else:
                if set(payload) != {
                    "approval_id",
                    "approval_fingerprint",
                    "revoked_at",
                    "revocation_evidence_sha256",
                }:
                    raise ExecutionLedgerIntegrityError(
                        "supervised approval revocation schema is invalid"
                    )
                prior = approval_state.get(event["plan_id"])
                if prior is None:
                    raise ExecutionLedgerIntegrityError(
                        "supervised approval revocation lacks binding"
                    )
                try:
                    _sha256_text(payload["approval_fingerprint"], "approval_fingerprint")
                    _sha256_text(
                        payload["revocation_evidence_sha256"],
                        "revocation_evidence_sha256",
                    )
                    revoked_at = _timestamp(payload["revoked_at"], "revoked_at")
                except (KeyError, TypeError, ValueError) as exc:
                    raise ExecutionLedgerIntegrityError(
                        "supervised approval revocation values are invalid"
                    ) from exc
                if (
                    payload["approval_id"] != prior[0]
                    or payload["approval_fingerprint"] != prior[1]
                    or revoked_at < prior[2]
                ):
                    raise ExecutionLedgerIntegrityError(
                        "supervised approval revocation identity/chronology mismatch"
                    )
                approval_state[event["plan_id"]] = (
                    prior[0],
                    prior[1],
                    prior[2],
                    True,
                )

        attempt_ids = {
            event["attempt_id"]
            for event in events
            if event["event_type"] == EventType.ATTEMPT_RESERVED.value
        }
        for attempt_id in attempt_ids:
            attempt_events = cls._attempt_events(events, attempt_id)
            first = attempt_events[0]
            if first["event_type"] != EventType.ATTEMPT_RESERVED.value:
                raise ExecutionLedgerIntegrityError(
                    "attempt history does not begin with reservation"
                )
            if set(first["payload"]) != {"effect_fingerprint", "reserved_at"}:
                raise ExecutionLedgerIntegrityError(
                    "ATTEMPT_RESERVED payload schema is invalid"
                )
            try:
                reserved_time = _timestamp(
                    first["payload"]["reserved_at"], "reserved_at"
                )
            except (KeyError, ValueError) as exc:
                raise ExecutionLedgerIntegrityError(
                    "attempt reservation timestamp is invalid"
                ) from exc
            plan_event, action = cls._action_payload(
                events, first["plan_id"], first["action_id"]
            )
            try:
                expires_time = _timestamp(action["expires_at"], "expires_at")
            except (KeyError, ValueError) as exc:
                raise ExecutionLedgerIntegrityError(
                    "stored action expiry timestamp is invalid"
                ) from exc
            if reserved_time >= expires_time:
                raise ExecutionLedgerIntegrityError(
                    "attempt reservation is at/after persisted quote expiry"
                )
            expected = _digest(
                {
                    "plan_fingerprint": plan_event["payload"][
                        "plan_fingerprint"
                    ],
                    "action": action,
                }
            )
            if first["payload"]["effect_fingerprint"] != expected:
                raise ExecutionLedgerIntegrityError(
                    "stored effect fingerprint mismatch"
                )
            submitted_time: datetime | None = None
            unknown_time: datetime | None = None
            provider_order_reference_seen = False
            found_reconciliations: dict[str, ExternalEffectReconciliation] = {}
            found_receipt_id: str | None = None
            for followup in attempt_events[1:]:
                if (
                    followup["plan_id"] != first["plan_id"]
                    or followup["action_id"] != first["action_id"]
                ):
                    raise ExecutionLedgerIntegrityError(
                        "attempt identity changes across history"
                    )
                try:
                    if (
                        followup["event_type"]
                        == EventType.ATTEMPT_SUBMITTED.value
                    ):
                        submitted_time = _timestamp(
                            followup["payload"]["submitted_at"],
                            "submitted_at",
                        )
                        if submitted_time < reserved_time:
                            raise ExecutionLedgerIntegrityError(
                                "attempt submission precedes reservation"
                            )
                        if submitted_time >= expires_time:
                            raise ExecutionLedgerIntegrityError(
                                "attempt submission is at/after persisted quote expiry"
                            )
                    elif (
                        followup["event_type"]
                        == EventType.ATTEMPT_UNKNOWN.value
                    ):
                        unknown_time = _timestamp(
                            followup["payload"]["observed_at"],
                            "observed_at",
                        )
                        if unknown_time < reserved_time:
                            raise ExecutionLedgerIntegrityError(
                                "UNKNOWN observation precedes attempt reservation"
                            )
                        if (
                            submitted_time is not None
                            and unknown_time < submitted_time
                        ):
                            raise ExecutionLedgerIntegrityError(
                                "UNKNOWN observation precedes attempt submission"
                            )
                    elif (
                        followup["event_type"]
                        == EventType.RECONCILED_FOUND.value
                    ):
                        reconciliation = cls._found_reconciliation_from_dict(
                            followup["payload"]
                        )
                        if reconciliation.attempt_id != attempt_id:
                            raise ExecutionLedgerIntegrityError(
                                "found reconciliation attempt identity mismatch"
                            )
                        if unknown_time is None:
                            raise ExecutionLedgerIntegrityError(
                                "found reconciliation lacks prior UNKNOWN boundary"
                            )
                        observed_time = _timestamp(
                            reconciliation.observed_at, "observed_at"
                        )
                        causal_boundaries = [reserved_time, unknown_time]
                        if submitted_time is not None:
                            causal_boundaries.append(submitted_time)
                        if observed_time <= max(causal_boundaries):
                            raise ExecutionLedgerIntegrityError(
                                "found reconciliation is not newer than "
                                "attempt causal boundary"
                            )
                        prior = found_reconciliations.get(
                            reconciliation.evidence_id
                        )
                        if prior is not None and prior != reconciliation:
                            raise ExecutionLedgerIntegrityError(
                                "conflicting found reconciliation evidence"
                            )
                        if (
                            found_receipt_id is not None
                            and found_receipt_id
                            != reconciliation.external_receipt_id
                        ):
                            raise ExecutionLedgerIntegrityError(
                                "conflicting found reconciliation receipt identity"
                            )
                        found_receipt_id = reconciliation.external_receipt_id
                        found_reconciliations[
                            reconciliation.evidence_id
                        ] = reconciliation
                    elif (
                        followup["event_type"]
                        == EventType.PROVIDER_ORDER_REFERENCE_BOUND.value
                    ):
                        if provider_order_reference_seen:
                            raise ExecutionLedgerIntegrityError(
                                "attempt has multiple provider order reference bindings"
                            )
                        provider_order_reference_seen = True
                        if submitted_time is not None:
                            raise ExecutionLedgerIntegrityError(
                                "provider order reference was bound after submission"
                            )
                        if set(followup["payload"]) != {
                            "provider_id",
                            "account_id",
                            "provider_order_ref",
                            "binding_sha256",
                        }:
                            raise ExecutionLedgerIntegrityError(
                                "provider order reference binding schema is invalid"
                            )
                        provider_id = _text(
                            followup["payload"]["provider_id"], "provider_id"
                        )
                        account_id = _text(
                            followup["payload"]["account_id"], "account_id"
                        )
                        provider_order_ref = _text(
                            followup["payload"]["provider_order_ref"],
                            "provider_order_ref",
                        )
                        binding_sha256 = _sha256_text(
                            followup["payload"]["binding_sha256"],
                            "binding_sha256",
                        )
                        if provider_id != action["bookmaker_id"] or account_id != action["account_id"]:
                            raise ExecutionLedgerIntegrityError(
                                "provider order reference authority mismatches action"
                            )
                        expected_binding = _digest(
                            {
                                "schema": "autosport.provider_order_reference_binding",
                                "schema_version": 1,
                                "provider_id": provider_id,
                                "account_id": account_id,
                                "plan_id": first["plan_id"],
                                "action_id": first["action_id"],
                                "attempt_id": attempt_id,
                                "effect_fingerprint": first["payload"]["effect_fingerprint"],
                            }
                        )
                        expected_ref = hashlib.sha256(
                            f"{provider_id}:{account_id}:{expected_binding}".encode("utf-8")
                        ).hexdigest()[:32]
                        if binding_sha256 != expected_binding or provider_order_ref != expected_ref:
                            raise ExecutionLedgerIntegrityError(
                                "provider order reference binding mismatch"
                            )
                    elif (
                        followup["event_type"]
                        == EventType.PROVIDER_EVIDENCE_BOUND.value
                    ):
                        if set(followup["payload"]) != {
                            "evidence_id",
                            "observed_at",
                            "source",
                        }:
                            raise ExecutionLedgerIntegrityError(
                                "provider evidence binding schema is invalid"
                            )
                        _sha256_text(
                            followup["payload"]["evidence_id"], "evidence_id"
                        )
                        _text(followup["payload"]["source"], "source")
                        evidence_time = _timestamp(
                            followup["payload"]["observed_at"], "observed_at"
                        )
                        causal_boundaries = [reserved_time]
                        if submitted_time is not None:
                            causal_boundaries.append(submitted_time)
                        if unknown_time is not None:
                            causal_boundaries.append(unknown_time)
                        if evidence_time < max(causal_boundaries):
                            raise ExecutionLedgerIntegrityError(
                                "provider evidence precedes attempt causal boundary"
                            )
                    elif (
                        followup["event_type"]
                        == EventType.EXTERNAL_ACKNOWLEDGEMENT.value
                    ):
                        acknowledgement = cls._acknowledgement_from_dict(
                            followup["payload"]
                        )
                        if acknowledgement.attempt_id != attempt_id:
                            raise ExecutionLedgerIntegrityError(
                                "stored acknowledgement attempt identity mismatch"
                            )
                        acknowledged_time = _timestamp(
                            acknowledgement.acknowledged_at,
                            "acknowledged_at",
                        )
                        causal_boundaries = [reserved_time]
                        if submitted_time is not None:
                            causal_boundaries.append(submitted_time)
                        if unknown_time is not None:
                            causal_boundaries.append(unknown_time)
                        if acknowledged_time < max(causal_boundaries):
                            raise ExecutionLedgerIntegrityError(
                                "acknowledgement precedes attempt causal boundary"
                            )
                        evidence_id = acknowledgement.reconciliation_evidence_id
                        if unknown_time is not None:
                            if not evidence_id:
                                raise ExecutionLedgerIntegrityError(
                                    "UNKNOWN acknowledgement lacks reconciliation evidence"
                                )
                            reconciliation = found_reconciliations.get(evidence_id)
                            if reconciliation is None:
                                raise ExecutionLedgerIntegrityError(
                                    "UNKNOWN acknowledgement references missing "
                                    "reconciliation evidence"
                                )
                            if (
                                reconciliation.external_receipt_id
                                != acknowledgement.external_receipt_id
                            ):
                                raise ExecutionLedgerIntegrityError(
                                    "UNKNOWN acknowledgement receipt mismatches "
                                    "reconciliation evidence"
                                )
                            evidence_time = _timestamp(
                                reconciliation.observed_at, "observed_at"
                            )
                            if acknowledged_time < evidence_time:
                                raise ExecutionLedgerIntegrityError(
                                    "acknowledgement precedes reconciliation evidence"
                                )
                        elif evidence_id:
                            raise ExecutionLedgerIntegrityError(
                                "reconciliation evidence is only valid for "
                                "UNKNOWN acknowledgement"
                            )
                        if acknowledgement.accepted_stake is not None:
                            requested_stake = _decimal(
                                action["requested_stake"], "requested_stake"
                            )
                            if acknowledgement.accepted_stake > requested_stake:
                                raise ExecutionLedgerIntegrityError(
                                    "stored acknowledgement stake exceeds "
                                    "requested action stake"
                                )
                    elif (
                        followup["event_type"]
                        == EventType.RECONCILED_NOT_FOUND.value
                    ):
                        snapshot = cls._reconciliation_snapshot_from_dict(
                            followup["payload"]
                        )
                        if snapshot.attempt_id != attempt_id:
                            raise ExecutionLedgerIntegrityError(
                                "not-found reconciliation attempt identity mismatch"
                            )
                        if snapshot.external_effect_found:
                            raise ExecutionLedgerIntegrityError(
                                "stored not-found reconciliation cannot claim external effect"
                            )
                        reconciled_time = _timestamp(
                            snapshot.observed_at,
                            "observed_at",
                        )
                        causal_boundaries = [reserved_time]
                        if submitted_time is not None:
                            causal_boundaries.append(submitted_time)
                        if unknown_time is not None:
                            causal_boundaries.append(unknown_time)
                        if reconciled_time <= max(causal_boundaries):
                            raise ExecutionLedgerIntegrityError(
                                "not-found reconciliation is not newer than "
                                "attempt causal boundary"
                            )
                except (KeyError, ValueError) as exc:
                    raise ExecutionLedgerIntegrityError(
                        "attempt chronology timestamp is invalid"
                    ) from exc
            cls._state(attempt_events)

        provider_reference_owners: dict[tuple[str, str, str], str] = {}
        for event in events:
            if event["event_type"] != EventType.PROVIDER_ORDER_REFERENCE_BOUND.value:
                continue
            payload = event["payload"]
            key = (
                payload["provider_id"],
                payload["account_id"],
                payload["provider_order_ref"],
            )
            prior_owner = provider_reference_owners.get(key)
            if prior_owner is not None and prior_owner != event["attempt_id"]:
                raise ExecutionLedgerIntegrityError(
                    "provider order reference belongs to multiple attempts"
                )
            provider_reference_owners[key] = event["attempt_id"]

        for event in events:
            if event["event_type"] in {
                EventType.PLAN_RESERVED.value,
                EventType.SUPERVISED_APPROVAL_BOUND.value,
                EventType.SUPERVISED_APPROVAL_REVOKED.value,
            }:
                continue
            if event["attempt_id"] not in attempt_ids:
                raise ExecutionLedgerIntegrityError(
                    "attempt event lacks reservation"
                )
            if event["plan_id"] not in plan_ids:
                raise ExecutionLedgerIntegrityError(
                    "attempt event references missing plan"
                )
        cls._receipt_owners(events)

    def bind_supervised_approval(
        self,
        *,
        plan_id: str,
        approval_id: str,
        approval_fingerprint: str,
        approved_at: str,
        evidence_sha256: str,
    ) -> None:
        _text(approval_id, "approval_id")
        _sha256_text(approval_fingerprint, "approval_fingerprint")
        _timestamp(approved_at, "approved_at")
        _sha256_text(evidence_sha256, "evidence_sha256")

        def operation() -> None:
            events = self._events()
            plan_event = self._plan_event(events, plan_id)
            if plan_event is None:
                raise ExecutionStateError("approval binding requires reserved plan")
            if plan_event["payload"]["plan"]["approval_id"] != approval_id:
                raise ExecutionIdentityConflict(
                    "approval identity mismatches durable execution plan"
                )
            bindings = [
                event
                for event in events
                if event["plan_id"] == plan_id
                and event["event_type"] == EventType.SUPERVISED_APPROVAL_BOUND.value
            ]
            payload = {
                "approval_id": approval_id,
                "approval_fingerprint": approval_fingerprint,
                "approved_at": approved_at,
                "evidence_sha256": evidence_sha256,
            }
            if bindings:
                if len(bindings) == 1 and bindings[0]["payload"] == payload:
                    return
                raise ExecutionIdentityConflict(
                    "durable supervised approval binding conflicts"
                )
            self._append(
                EventType.SUPERVISED_APPROVAL_BOUND,
                plan_id,
                None,
                None,
                payload,
            )

        self._mutate(operation)

    def revoke_supervised_approval(
        self,
        *,
        plan_id: str,
        approval_id: str,
        approval_fingerprint: str,
        revoked_at: str,
        revocation_evidence_sha256: str,
    ) -> None:
        _text(approval_id, "approval_id")
        _sha256_text(approval_fingerprint, "approval_fingerprint")
        _timestamp(revoked_at, "revoked_at")
        _sha256_text(revocation_evidence_sha256, "revocation_evidence_sha256")

        def operation() -> None:
            events = self._events()
            bindings = [
                event
                for event in events
                if event["plan_id"] == plan_id
                and event["event_type"] == EventType.SUPERVISED_APPROVAL_BOUND.value
            ]
            if len(bindings) != 1:
                raise ExecutionStateError(
                    "approval revocation requires one durable approval binding"
                )
            binding = bindings[0]["payload"]
            if (
                binding["approval_id"] != approval_id
                or binding["approval_fingerprint"] != approval_fingerprint
            ):
                raise ExecutionIdentityConflict(
                    "approval revocation identity mismatches durable binding"
                )
            if _timestamp(revoked_at, "revoked_at") < _timestamp(
                binding["approved_at"], "approved_at"
            ):
                raise ExecutionStateError(
                    "approval revocation predates durable approval"
                )
            payload = {
                "approval_id": approval_id,
                "approval_fingerprint": approval_fingerprint,
                "revoked_at": revoked_at,
                "revocation_evidence_sha256": revocation_evidence_sha256,
            }
            revocations = [
                event
                for event in events
                if event["plan_id"] == plan_id
                and event["event_type"]
                == EventType.SUPERVISED_APPROVAL_REVOKED.value
            ]
            if revocations:
                if len(revocations) == 1 and revocations[0]["payload"] == payload:
                    return
                raise ExecutionIdentityConflict(
                    "durable supervised approval has conflicting revocation"
                )
            self._append(
                EventType.SUPERVISED_APPROVAL_REVOKED,
                plan_id,
                None,
                None,
                payload,
            )

        self._mutate(operation)

    def supervised_approval_is_active(
        self,
        *,
        plan_id: str,
        approval_id: str,
        approval_fingerprint: str,
    ) -> bool:
        events = self._events()
        bindings = [
            event
            for event in events
            if event["plan_id"] == plan_id
            and event["event_type"] == EventType.SUPERVISED_APPROVAL_BOUND.value
        ]
        if len(bindings) != 1:
            return False
        payload = bindings[0]["payload"]
        if (
            payload["approval_id"] != approval_id
            or payload["approval_fingerprint"] != approval_fingerprint
        ):
            return False
        return not any(
            event["plan_id"] == plan_id
            and event["event_type"] == EventType.SUPERVISED_APPROVAL_REVOKED.value
            for event in events
        )

    def bind_provider_order_reference(
        self,
        *,
        attempt_id: str,
        provider_id: str,
    ) -> str:
        """Durably bind a provider-safe <=32-char order reference before submission."""
        _text(attempt_id, "attempt_id")
        provider = _text(provider_id, "provider_id")

        def operation() -> str:
            events = self._events()
            attempt_events = self._attempt_events(events, attempt_id)
            if not attempt_events:
                raise ExecutionStateError(
                    "provider order reference requires reserved attempt"
                )
            first = attempt_events[0]
            _, action = self._action_payload(
                events, first["plan_id"], first["action_id"]
            )
            if provider != action["bookmaker_id"]:
                raise ExecutionIdentityConflict(
                    "provider order reference authority mismatches action bookmaker"
                )
            account_id = _text(action["account_id"], "account_id")
            binding_sha256 = _digest(
                {
                    "schema": "autosport.provider_order_reference_binding",
                    "schema_version": 1,
                    "provider_id": provider,
                    "account_id": account_id,
                    "plan_id": first["plan_id"],
                    "action_id": first["action_id"],
                    "attempt_id": attempt_id,
                    "effect_fingerprint": first["payload"]["effect_fingerprint"],
                }
            )
            provider_order_ref = hashlib.sha256(
                f"{provider}:{account_id}:{binding_sha256}".encode("utf-8")
            ).hexdigest()[:32]
            payload = {
                "provider_id": provider,
                "account_id": account_id,
                "provider_order_ref": provider_order_ref,
                "binding_sha256": binding_sha256,
            }
            existing = [
                event
                for event in attempt_events
                if event["event_type"]
                == EventType.PROVIDER_ORDER_REFERENCE_BOUND.value
            ]
            if existing:
                if len(existing) == 1 and existing[0]["payload"] == payload:
                    return provider_order_ref
                raise ExecutionIdentityConflict(
                    "attempt already has a different provider order reference"
                )
            if self._state(attempt_events) != AttemptState.RESERVED:
                raise ExecutionStateError(
                    "provider order reference must be bound before submission"
                )
            for event in events:
                if (
                    event["event_type"]
                    == EventType.PROVIDER_ORDER_REFERENCE_BOUND.value
                    and event["payload"].get("provider_id") == provider
                    and event["payload"].get("account_id") == account_id
                    and event["payload"].get("provider_order_ref") == provider_order_ref
                    and event["attempt_id"] != attempt_id
                ):
                    raise ExecutionIdentityConflict(
                        "provider order reference collision across attempts"
                    )
            self._append(
                EventType.PROVIDER_ORDER_REFERENCE_BOUND,
                first["plan_id"],
                first["action_id"],
                attempt_id,
                payload,
            )
            return provider_order_ref

        return self._mutate(operation)

    def provider_order_reference(
        self,
        *,
        attempt_id: str,
        provider_id: str,
    ) -> str | None:
        provider = _text(provider_id, "provider_id")
        events = self._events()
        matches = [
            event["payload"]
            for event in self._attempt_events(events, attempt_id)
            if event["event_type"]
            == EventType.PROVIDER_ORDER_REFERENCE_BOUND.value
            and event["payload"].get("provider_id") == provider
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ExecutionLedgerIntegrityError(
                "attempt has multiple provider order reference bindings"
            )
        value = matches[0].get("provider_order_ref")
        if not isinstance(value, str):
            raise ExecutionLedgerIntegrityError(
                "provider order reference binding is invalid"
            )
        return value

    def bind_provider_evidence(
        self,
        *,
        attempt_id: str,
        evidence_id: str,
        observed_at: str,
        source: str,
    ) -> None:
        _text(attempt_id, "attempt_id")
        _sha256_text(evidence_id, "evidence_id")
        _timestamp(observed_at, "observed_at")
        _text(source, "source")

        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(events, attempt_id)
            if not attempt_events:
                raise ExecutionStateError(
                    "provider evidence requires reserved attempt"
                )
            payload = {
                "evidence_id": evidence_id,
                "observed_at": observed_at,
                "source": source,
            }
            existing = [
                event
                for event in attempt_events
                if event["event_type"] == EventType.PROVIDER_EVIDENCE_BOUND.value
            ]
            if existing:
                if len(existing) == 1 and existing[0]["payload"] == payload:
                    return
                raise ExecutionIdentityConflict(
                    "attempt already has different provider evidence"
                )
            state = self._state(attempt_events)
            if state not in {AttemptState.SUBMITTED, AttemptState.UNKNOWN}:
                raise ExecutionStateError(
                    "new provider evidence requires SUBMITTED/UNKNOWN attempt"
                )
            first = attempt_events[0]
            boundaries = [
                _timestamp(first["payload"]["reserved_at"], "reserved_at")
            ]
            for event in attempt_events:
                if event["event_type"] == EventType.ATTEMPT_SUBMITTED.value:
                    boundaries.append(
                        _timestamp(event["payload"]["submitted_at"], "submitted_at")
                    )
                elif event["event_type"] == EventType.ATTEMPT_UNKNOWN.value:
                    boundaries.append(
                        _timestamp(event["payload"]["observed_at"], "observed_at")
                    )
            if _timestamp(observed_at, "observed_at") < max(boundaries):
                raise ExecutionStateError(
                    "provider evidence precedes attempt causal boundary"
                )
            self._append(
                EventType.PROVIDER_EVIDENCE_BOUND,
                first["plan_id"],
                first["action_id"],
                attempt_id,
                payload,
            )

        self._mutate(operation)

    def provider_evidence_binding(
        self,
        attempt_id: str,
    ) -> dict[str, str] | None:
        events = self._events()
        matches = [
            event["payload"]
            for event in self._attempt_events(events, attempt_id)
            if event["event_type"] == EventType.PROVIDER_EVIDENCE_BOUND.value
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ExecutionLedgerIntegrityError(
                "attempt has multiple provider evidence bindings"
            )
        return dict(matches[0])

    def reserve_plan(self, plan: ExecutionPlan) -> str:
        def operation() -> str:
            events = self._events()
            prior = self._plan_event(events, plan.plan_id)
            if prior:
                if (
                    prior["payload"]["plan_fingerprint"]
                    != plan.fingerprint
                ):
                    raise ExecutionIdentityConflict(
                        "plan_id reused with different immutable inputs"
                    )
                return plan.fingerprint
            self._append(
                EventType.PLAN_RESERVED,
                plan.plan_id,
                None,
                None,
                {
                    "plan_fingerprint": plan.fingerprint,
                    "plan": plan.to_dict(),
                },
            )
            return plan.fingerprint

        return self._mutate(operation)

    def begin_attempt(
        self,
        *,
        plan_id: str,
        action_id: str,
        attempt_id: str,
        reserved_at: str | None = None,
    ) -> ExecutionAttempt:
        _text(attempt_id, "attempt_id")
        reserved_at = reserved_at or _now()
        _timestamp(reserved_at, "reserved_at")

        def operation() -> ExecutionAttempt:
            events = self._events()
            plan_event, action = self._action_payload(
                events, plan_id, action_id
            )
            if self._stale(events, plan_id):
                raise ExecutionStateError(
                    "execution plan is stale; recompute before another action"
                )
            fingerprint = _digest(
                {
                    "plan_fingerprint": plan_event["payload"][
                        "plan_fingerprint"
                    ],
                    "action": action,
                }
            )
            prior_events = self._attempt_events(events, attempt_id)
            if prior_events:
                prior = prior_events[0]
                if (
                    prior["plan_id"],
                    prior["action_id"],
                    prior["payload"].get("effect_fingerprint"),
                ) != (plan_id, action_id, fingerprint):
                    raise ExecutionIdentityConflict(
                        "attempt_id reused with different immutable inputs"
                    )
                return ExecutionAttempt(
                    attempt_id,
                    plan_id,
                    action_id,
                    fingerprint,
                    prior["payload"]["reserved_at"],
                )
            for event in events:
                if (
                    event["plan_id"] == plan_id
                    and event["action_id"] == action_id
                    and event["event_type"]
                    == EventType.ATTEMPT_RESERVED.value
                ):
                    if (
                        self._state(
                            self._attempt_events(events, event["attempt_id"])
                        )
                        != AttemptState.RECONCILED_NOT_FOUND
                    ):
                        raise ExecutionStateError(
                            "action already has unresolved/final attempt"
                        )
            if _timestamp(reserved_at, "reserved_at") >= _timestamp(
                action["expires_at"], "expires_at"
            ):
                raise ExecutionStateError(
                    "cannot reserve attempt at or after persisted quote expiry"
                )
            self._append(
                EventType.ATTEMPT_RESERVED,
                plan_id,
                action_id,
                attempt_id,
                {
                    "effect_fingerprint": fingerprint,
                    "reserved_at": reserved_at,
                },
            )
            return ExecutionAttempt(
                attempt_id, plan_id, action_id, fingerprint, reserved_at
            )

        return self._mutate(operation)

    def mark_submitted(
        self, attempt_id: str, submitted_at: str | None = None
    ) -> None:
        actual_submitted_at = submitted_at or _now()
        submitted_time = _timestamp(actual_submitted_at, "submitted_at")

        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(events, attempt_id)
            state = self._state(attempt_events)
            if state == AttemptState.SUBMITTED:
                return
            if state != AttemptState.RESERVED:
                raise ExecutionStateError(
                    "only reserved attempt can be submitted"
                )
            first = attempt_events[0]
            reserved_time = _timestamp(
                first["payload"]["reserved_at"], "reserved_at"
            )
            if submitted_time < reserved_time:
                raise ExecutionStateError(
                    "submitted_at cannot precede attempt reservation"
                )
            _, action = self._action_payload(
                events, first["plan_id"], first["action_id"]
            )
            if submitted_time >= _timestamp(
                action["expires_at"], "expires_at"
            ):
                raise ExecutionStateError(
                    "cannot submit attempt at or after persisted quote expiry"
                )
            self._append(
                EventType.ATTEMPT_SUBMITTED,
                first["plan_id"],
                first["action_id"],
                attempt_id,
                {"submitted_at": actual_submitted_at},
            )

        self._mutate(operation)

    def mark_unknown(
        self,
        attempt_id: str,
        *,
        reason: str,
        observed_at: str | None = None,
    ) -> None:
        _text(reason, "reason")
        actual_observed_at = observed_at or _now()
        observed_time = _timestamp(actual_observed_at, "observed_at")

        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(events, attempt_id)
            state = self._state(attempt_events)
            if state == AttemptState.UNKNOWN:
                return
            if state not in {
                AttemptState.RESERVED,
                AttemptState.SUBMITTED,
            }:
                raise ExecutionStateError(
                    "only unresolved attempt can become UNKNOWN"
                )
            first = attempt_events[0]
            reserved_time = _timestamp(
                first["payload"]["reserved_at"], "reserved_at"
            )
            if observed_time < reserved_time:
                raise ExecutionStateError(
                    "UNKNOWN observed_at cannot precede attempt reservation"
                )
            submitted_events = [
                event
                for event in attempt_events
                if event["event_type"]
                == EventType.ATTEMPT_SUBMITTED.value
            ]
            if submitted_events:
                submitted_time = _timestamp(
                    submitted_events[-1]["payload"]["submitted_at"],
                    "submitted_at",
                )
                if observed_time < submitted_time:
                    raise ExecutionStateError(
                        "UNKNOWN observed_at cannot precede attempt submission"
                    )
            self._append(
                EventType.ATTEMPT_UNKNOWN,
                first["plan_id"],
                first["action_id"],
                attempt_id,
                {
                    "reason": reason,
                    "observed_at": actual_observed_at,
                },
            )

        self._mutate(operation)

    def recover_uncertain(
        self, *, reason: str = "process_restart"
    ) -> tuple[str, ...]:
        _text(reason, "reason")

        def operation() -> tuple[str, ...]:
            promoted: list[str] = []
            events = self._events()
            reserved_events = [
                event
                for event in events
                if event["event_type"]
                == EventType.ATTEMPT_RESERVED.value
            ]
            for reserved in reserved_events:
                attempt_id = reserved["attempt_id"]
                if self._state(
                    self._attempt_events(events, attempt_id)
                ) in {AttemptState.RESERVED, AttemptState.SUBMITTED}:
                    self._append(
                        EventType.ATTEMPT_UNKNOWN,
                        reserved["plan_id"],
                        reserved["action_id"],
                        attempt_id,
                        {"reason": reason, "observed_at": _now()},
                    )
                    promoted.append(attempt_id)
                    events = self._events()
            return tuple(promoted)

        return self._mutate(operation)

    def acknowledge(
        self, acknowledgement: ExternalAcknowledgement
    ) -> None:
        payload = acknowledgement.to_dict()

        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(
                events, acknowledgement.attempt_id
            )
            existing = [
                event
                for event in attempt_events
                if event["event_type"]
                == EventType.EXTERNAL_ACKNOWLEDGEMENT.value
            ]
            if existing:
                if existing[0]["payload"] == payload:
                    return
                raise ExecutionIdentityConflict(
                    "attempt already has a different acknowledgement"
                )
            state = self._state(attempt_events)
            if state not in {
                AttemptState.SUBMITTED,
                AttemptState.UNKNOWN,
            }:
                raise ExecutionStateError(
                    "acknowledgement requires durable submission or "
                    "UNKNOWN reconciliation"
                )
            first = attempt_events[0]
            causal_boundaries: list[datetime] = [
                _timestamp(first["payload"]["reserved_at"], "reserved_at")
            ]
            for event in attempt_events:
                if event["event_type"] == EventType.ATTEMPT_SUBMITTED.value:
                    causal_boundaries.append(
                        _timestamp(event["payload"]["submitted_at"], "submitted_at")
                    )
                elif event["event_type"] == EventType.ATTEMPT_UNKNOWN.value:
                    causal_boundaries.append(
                        _timestamp(event["payload"]["observed_at"], "observed_at")
                    )
            acknowledged_time = _timestamp(
                acknowledgement.acknowledged_at, "acknowledged_at"
            )
            if acknowledged_time < max(causal_boundaries):
                raise ExecutionStateError(
                    "acknowledgement precedes attempt causal boundary"
                )
            if state == AttemptState.UNKNOWN:
                evidence_id = acknowledgement.reconciliation_evidence_id
                if not evidence_id:
                    raise ExecutionStateError(
                        "UNKNOWN attempt requires external reconciliation evidence"
                    )
                matching = [
                    event
                    for event in attempt_events
                    if event["event_type"] == EventType.RECONCILED_FOUND.value
                    and event["payload"].get("evidence_id") == evidence_id
                ]
                if len(matching) != 1:
                    raise ExecutionStateError(
                        "UNKNOWN acknowledgement requires durable positive "
                        "reconciliation evidence"
                    )
                reconciliation = self._found_reconciliation_from_dict(
                    matching[0]["payload"]
                )
                if (
                    reconciliation.external_receipt_id
                    != acknowledgement.external_receipt_id
                ):
                    raise ExecutionStateError(
                        "acknowledgement receipt mismatches reconciliation evidence"
                    )
                evidence_time = _timestamp(
                    reconciliation.observed_at, "observed_at"
                )
                if evidence_time <= max(causal_boundaries):
                    raise ExecutionStateError(
                        "positive reconciliation evidence must be newer than "
                        "attempt uncertainty boundary"
                    )
                if acknowledged_time < evidence_time:
                    raise ExecutionStateError(
                        "acknowledgement precedes reconciliation evidence"
                    )
            elif acknowledgement.reconciliation_evidence_id:
                raise ExecutionStateError(
                    "reconciliation evidence is only valid for UNKNOWN acknowledgement"
                )
            if acknowledgement.accepted_stake is not None:
                _, action = self._action_payload(
                    events, first["plan_id"], first["action_id"]
                )
                requested_stake = _decimal(
                    action["requested_stake"], "requested_stake"
                )
                if acknowledgement.accepted_stake > requested_stake:
                    raise ExecutionStateError(
                        "acknowledged stake exceeds requested action stake"
                    )
            probe_event = {
                "plan_id": first["plan_id"],
                "action_id": first["action_id"],
                "attempt_id": acknowledgement.attempt_id,
                "payload": payload,
            }
            identity = self._receipt_identity(events, probe_event)
            prior = self._receipt_owners(events).get(identity)
            if prior is not None and prior != acknowledgement.attempt_id:
                raise ExecutionIdentityConflict(
                    "provider/account external receipt already belongs to another attempt"
                )
            self._append(
                EventType.EXTERNAL_ACKNOWLEDGEMENT,
                first["plan_id"],
                first["action_id"],
                acknowledgement.attempt_id,
                payload,
            )

        self._mutate(operation)

    def reconcile_found(
        self, reconciliation: ExternalEffectReconciliation
    ) -> None:
        payload = reconciliation.to_dict()

        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(
                events, reconciliation.attempt_id
            )
            if self._state(attempt_events) != AttemptState.UNKNOWN:
                raise ExecutionStateError(
                    "positive reconciliation requires UNKNOWN attempt"
                )
            same_evidence = [
                event
                for event in attempt_events
                if event["event_type"] == EventType.RECONCILED_FOUND.value
                and event["payload"].get("evidence_id")
                == reconciliation.evidence_id
            ]
            if same_evidence:
                if len(same_evidence) == 1 and same_evidence[0]["payload"] == payload:
                    return
                raise ExecutionIdentityConflict(
                    "different positive reconciliation evidence already exists"
                )
            for event in attempt_events:
                if event["event_type"] != EventType.RECONCILED_FOUND.value:
                    continue
                existing_reconciliation = self._found_reconciliation_from_dict(
                    event["payload"]
                )
                if (
                    existing_reconciliation.external_receipt_id
                    != reconciliation.external_receipt_id
                ):
                    raise ExecutionIdentityConflict(
                        "conflicting positive reconciliation receipt identity"
                    )
            causal_boundaries: list[datetime] = [
                _timestamp(
                    attempt_events[0]["payload"]["reserved_at"], "reserved_at"
                )
            ]
            for event in attempt_events:
                if event["event_type"] == EventType.ATTEMPT_SUBMITTED.value:
                    causal_boundaries.append(
                        _timestamp(
                            event["payload"]["submitted_at"], "submitted_at"
                        )
                    )
                elif event["event_type"] == EventType.ATTEMPT_UNKNOWN.value:
                    causal_boundaries.append(
                        _timestamp(
                            event["payload"]["observed_at"], "observed_at"
                        )
                    )
            if _timestamp(
                reconciliation.observed_at, "observed_at"
            ) <= max(causal_boundaries):
                raise ExecutionStateError(
                    "positive reconciliation evidence must be newer than "
                    "attempt uncertainty boundary"
                )
            first = attempt_events[0]
            probe_event = {
                "plan_id": first["plan_id"],
                "action_id": first["action_id"],
                "attempt_id": reconciliation.attempt_id,
                "payload": payload,
            }
            identity = self._receipt_identity(events, probe_event)
            prior = self._receipt_owners(events).get(identity)
            if prior is not None and prior != reconciliation.attempt_id:
                raise ExecutionIdentityConflict(
                    "provider/account external receipt already belongs to another attempt"
                )
            self._append(
                EventType.RECONCILED_FOUND,
                first["plan_id"],
                first["action_id"],
                reconciliation.attempt_id,
                payload,
            )

        self._mutate(operation)

    def reconcile_not_found(
        self, snapshot: ReconciliationSnapshot
    ) -> None:
        if snapshot.external_effect_found:
            raise ValueError(
                "found external effect must be reconciled as acknowledgement"
            )
        payload = snapshot.to_dict()

        def operation() -> None:
            events = self._events()
            attempt_events = self._attempt_events(
                events, snapshot.attempt_id
            )
            existing = [
                event
                for event in attempt_events
                if event["event_type"]
                == EventType.RECONCILED_NOT_FOUND.value
            ]
            if existing:
                if existing[0]["payload"] == payload:
                    return
                raise ExecutionIdentityConflict(
                    "different reconciliation evidence already exists"
                )
            if self._state(attempt_events) != AttemptState.UNKNOWN:
                raise ExecutionStateError(
                    "retry requires UNKNOWN + external not-found evidence"
                )
            if any(
                event["event_type"] == EventType.RECONCILED_FOUND.value
                for event in attempt_events
            ):
                raise ExecutionStateError(
                    "positive reconciliation evidence blocks not-found retry"
                )
            uncertainty_boundaries: list[datetime] = [
                _timestamp(
                    attempt_events[0]["payload"]["reserved_at"], "reserved_at"
                )
            ]
            for event in attempt_events:
                if event["event_type"] == EventType.ATTEMPT_SUBMITTED.value:
                    uncertainty_boundaries.append(
                        _timestamp(
                            event["payload"]["submitted_at"], "submitted_at"
                        )
                    )
                elif event["event_type"] == EventType.ATTEMPT_UNKNOWN.value:
                    uncertainty_boundaries.append(
                        _timestamp(
                            event["payload"]["observed_at"], "observed_at"
                        )
                    )
            if not uncertainty_boundaries:
                raise ExecutionLedgerIntegrityError(
                    "UNKNOWN attempt is missing an uncertainty boundary"
                )
            if _timestamp(snapshot.observed_at, "observed_at") <= max(
                uncertainty_boundaries
            ):
                raise ExecutionStateError(
                    "external not-found evidence must be newer than attempt uncertainty boundary"
                )
            first = attempt_events[0]
            self._append(
                EventType.RECONCILED_NOT_FOUND,
                first["plan_id"],
                first["action_id"],
                snapshot.attempt_id,
                payload,
            )

        self._mutate(operation)

    def verified_snapshot(self) -> VerifiedExecutionLedgerSnapshot:
        raw = self.path.read_bytes() if self.path.exists() else b""
        events = self._parse(raw)
        return VerifiedExecutionLedgerSnapshot(
            raw, hashlib.sha256(raw).hexdigest(), len(events)
        )

    def verify_integrity(self) -> int:
        return self.verified_snapshot().event_count

    def attempt_state(self, attempt_id: str) -> AttemptState:
        state = self._state(
            self._attempt_events(self._events(), attempt_id)
        )
        if state is None:
            raise KeyError(attempt_id)
        return state

    def plan_is_stale(self, plan_id: str) -> bool:
        events = self._events()
        if self._plan_event(events, plan_id) is None:
            raise KeyError(plan_id)
        return self._stale(events, plan_id)

    def can_retry_action(
        self, *, plan_id: str, action_id: str
    ) -> bool:
        events = self._events()
        self._action_payload(events, plan_id, action_id)
        if self._stale(events, plan_id):
            return False
        attempts = [
            event["attempt_id"]
            for event in events
            if event["plan_id"] == plan_id
            and event["action_id"] == action_id
            and event["event_type"]
            == EventType.ATTEMPT_RESERVED.value
        ]
        return (
            not attempts
            or self._state(
                self._attempt_events(events, attempts[-1])
            )
            == AttemptState.RECONCILED_NOT_FOUND
        )

    def saga(self, plan_id: str) -> ExecutionSaga:
        events = self._events()
        plan_event = self._plan_event(events, plan_id)
        if plan_event is None:
            raise KeyError(plan_id)
        attempts: dict[str, AttemptState] = {}
        action_ids: dict[str, str] = {}
        for event in events:
            if (
                event["plan_id"] == plan_id
                and event["event_type"]
                == EventType.ATTEMPT_RESERVED.value
            ):
                attempt_id = event["attempt_id"]
                state = self._state(
                    self._attempt_events(events, attempt_id)
                )
                if state is None:
                    raise ExecutionLedgerIntegrityError(
                        "reserved attempt has no state"
                    )
                attempts[attempt_id] = state
                action_ids[attempt_id] = event["action_id"]
        receipts = {
            identity: attempt
            for identity, attempt in self._receipt_owners(events).items()
            if attempt in attempts
        }
        return ExecutionSaga(
            plan_id,
            plan_event["payload"]["plan_fingerprint"],
            self._stale(events, plan_id),
            attempts,
            action_ids,
            receipts,
        )
