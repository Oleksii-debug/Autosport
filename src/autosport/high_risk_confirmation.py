from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Callable

from .json_integrity import strict_json_loads

_SCHEMA_VERSION = 1
_ZERO_HASH = "0" * 64
_MAX_OPERATOR_TEXT_BYTES = 4096
_MAX_TTL_SECONDS = 300
_FULL_AUDIT_INTERVAL = 64
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_TYPES = frozenset({"OPENED", "APPROVED", "CANCELLED", "CONSUMED"})
_INTERACTION_KINDS = frozenset({"OPEN_GESTURE", "DECISION_GESTURE", "CONSUME_REQUEST"})
_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "seq",
        "confirmation_id",
        "event_type",
        "action_id",
        "risk_class",
        "payload_sha256",
        "ui_session_id",
        "interaction_kind",
        "interaction_id",
        "operator_text",
        "opened_at",
        "expires_at",
        "event_at",
    }
)


class ConfirmationError(ValueError):
    """Base class for high-risk confirmation failures."""


class ConfirmationInputError(ConfirmationError):
    """Caller input is malformed or outside the bounded confirmation contract."""


class ConfirmationStateError(ConfirmationError):
    """The requested state transition is not currently authorized."""


class ConfirmationIntegrityError(ConfirmationError):
    """Persisted confirmation evidence is malformed, inconsistent, or corrupted."""


class ConfirmationStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    STALE_SESSION = "STALE_SESSION"


@dataclass(frozen=True, slots=True)
class ConfirmationDialogContract:
    role: str = "alertdialog"
    initial_focus_action: str = "cancel"
    default_action: str = "cancel"
    escape_action: str = "cancel"
    close_action: str = "cancel"
    approval_requires_separate_gesture: bool = True
    opening_gesture_grants_authority: bool = False


DIALOG_CONTRACT = ConfirmationDialogContract()


@dataclass(frozen=True, slots=True)
class ConfirmationPrompt:
    confirmation_id: str
    action_id: str
    risk_class: str
    payload_sha256: str
    ui_session_id: str
    opened_at: str
    expires_at: str
    operator_text: str
    dialog: ConfirmationDialogContract = DIALOG_CONTRACT


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    confirmation_id: str
    grant_id: str
    action_id: str
    risk_class: str
    payload_sha256: str
    ui_session_id: str
    approved_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ConfirmationView:
    confirmation_id: str
    action_id: str
    risk_class: str
    payload_sha256: str
    ui_session_id: str
    opened_at: str
    expires_at: str
    status: ConfirmationStatus
    approval_grant_id: str | None


@dataclass(frozen=True, slots=True)
class ConsumptionReceipt:
    confirmation_id: str
    grant_id: str
    action_id: str
    risk_class: str
    payload_sha256: str
    ui_session_id: str
    consumed_at: str
    audit_event_sha256: str


@dataclass(slots=True)
class _State:
    confirmation_id: str
    action_id: str
    risk_class: str
    payload_sha256: str
    ui_session_id: str
    opened_at: str
    expires_at: str
    operator_text: str
    status: ConfirmationStatus
    open_gesture_id: str
    approval_grant_id: str | None = None
    approved_at: str | None = None
    consumed_at: str | None = None


@dataclass(frozen=True, slots=True)
class _LoadedAudit:
    states: dict[str, _State]
    used_interactions: frozenset[str]
    tail_sha256: str
    event_count: int


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_token(field_name: str, value: object) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise ConfirmationInputError(
            f"{field_name} must be a 1..160 character ASCII identity token"
        )
    return value


def _validate_sha256(field_name: str, value: object) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise ConfirmationInputError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_operator_text(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ConfirmationInputError("operator_text must be non-empty text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ConfirmationInputError("operator_text must be valid UTF-8 text") from exc
    if len(encoded) > _MAX_OPERATOR_TEXT_BYTES:
        raise ConfirmationInputError(
            f"operator_text must not exceed {_MAX_OPERATOR_TEXT_BYTES} UTF-8 bytes"
        )
    for char in value:
        codepoint = ord(char)
        if (codepoint < 32 and char not in "\n\t") or codepoint == 127:
            raise ConfirmationInputError("operator_text contains a disallowed control character")
    return value


def _validate_ttl_seconds(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_TTL_SECONDS:
        raise ConfirmationInputError(
            f"ttl_seconds must be an integer in 1..{_MAX_TTL_SECONDS}"
        )
    return value


def _canonical_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ConfirmationInputError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc_text(value: object, field_name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ConfirmationIntegrityError(f"{field_name} must be canonical UTC-Z text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ConfirmationIntegrityError(f"{field_name} must be valid ISO-8601") from exc
    if _utc_text(parsed) != value:
        raise ConfirmationIntegrityError(f"{field_name} is not canonical UTC-Z text")
    return parsed


def _hash_event(prev_sha256: str, body_json: str) -> str:
    return hashlib.sha256((prev_sha256 + "\n" + body_json).encode("utf-8")).hexdigest()


def _new_session_id() -> str:
    return "session-" + secrets.token_hex(16)


def _new_confirmation_id() -> str:
    return "confirm-" + secrets.token_hex(16)


def _serialized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class HighRiskConfirmationStore:
    """Durable one-shot UI confirmation evidence; it never performs the high-risk action.

    A store instance owns one process/UI-session identity. Reopening the same SQLite file
    in a new instance creates a new session identity by default, so pre-restart PENDING or
    APPROVED confirmations cannot be consumed after restart.

    The SQLite hash chain is an integrity/audit mechanism, not an external anti-rollback
    trust root. Callers must keep irreversible execution, provider, risk and settlement
    authority in their existing canonical subsystems.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        session_id_factory: Callable[[], str] | None = None,
        confirmation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._confirmation_id_factory = confirmation_id_factory or _new_confirmation_id
        session_factory = session_id_factory or _new_session_id
        self.ui_session_id = _validate_token("ui_session_id", session_factory())
        self._lock = RLock()
        self._audit_cache: _LoadedAudit | None = None
        self._cache_checks = 0
        self._db_stamp: tuple[int, int] | None = None
        self._initialize_or_validate()

    def _now(self) -> datetime:
        return _canonical_utc(self._clock(), "clock result")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_or_validate(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if not tables and user_version == 0:
                connection.execute(
                    "CREATE TABLE confirmation_events ("
                    "seq INTEGER PRIMARY KEY,"
                    "body_json TEXT NOT NULL,"
                    "prev_sha256 TEXT NOT NULL,"
                    "event_sha256 TEXT NOT NULL"
                    ")"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX idx_confirmation_events_hash "
                    "ON confirmation_events(event_sha256)"
                )
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._validate_schema(connection)
            audit = self._load_audit(connection)
            connection.commit()
            self._set_cache(audit)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


    def _stat_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _set_cache(self, audit: _LoadedAudit) -> None:
        self._audit_cache = audit
        self._cache_checks = 0
        self._db_stamp = self._stat_stamp()

    def _current_audit(self, connection: sqlite3.Connection) -> _LoadedAudit:
        row = connection.execute(
            "SELECT seq,event_sha256 FROM confirmation_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        db_count = 0 if row is None else row[0]
        db_tail = _ZERO_HASH if row is None else row[1]
        cache = self._audit_cache
        stamp = self._stat_stamp()
        if (
            cache is not None
            and type(db_count) is int
            and type(db_tail) is str
            and db_count == cache.event_count
            and db_tail == cache.tail_sha256
            and stamp == self._db_stamp
            and self._cache_checks < _FULL_AUDIT_INTERVAL
        ):
            self._cache_checks += 1
            return cache
        audit = self._load_audit(connection)
        self._set_cache(audit)
        return audit

    def _advance_cache(
        self,
        audit: _LoadedAudit,
        *,
        interaction_id: str,
        event_sha256: str,
    ) -> None:
        advanced = _LoadedAudit(
            states=audit.states,
            used_interactions=audit.used_interactions | {interaction_id},
            tail_sha256=event_sha256,
            event_count=audit.event_count + 1,
        )
        self._set_cache(advanced)

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if type(user_version) is not int or user_version != _SCHEMA_VERSION:
            raise ConfirmationIntegrityError("confirmation database schema version mismatch")

        table_info = connection.execute(
            "PRAGMA table_info(confirmation_events)"
        ).fetchall()
        expected = [
            (0, "seq", "INTEGER", 0, None, 1),
            (1, "body_json", "TEXT", 1, None, 0),
            (2, "prev_sha256", "TEXT", 1, None, 0),
            (3, "event_sha256", "TEXT", 1, None, 0),
        ]
        if table_info != expected:
            raise ConfirmationIntegrityError("confirmation database table schema mismatch")

        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='confirmation_events'"
        ).fetchall()
        if triggers:
            raise ConfirmationIntegrityError("confirmation database triggers are not allowed")

        indexes = connection.execute(
            "PRAGMA index_list(confirmation_events)"
        ).fetchall()
        if len(indexes) != 1:
            raise ConfirmationIntegrityError("confirmation database index schema mismatch")
        index = indexes[0]
        if index[1] != "idx_confirmation_events_hash" or index[2] != 1:
            raise ConfirmationIntegrityError("confirmation database hash index mismatch")
        index_columns = connection.execute(
            "PRAGMA index_info(idx_confirmation_events_hash)"
        ).fetchall()
        if index_columns != [(0, 3, "event_sha256")]:
            raise ConfirmationIntegrityError("confirmation database hash index columns mismatch")

    def _validate_event_body(self, raw: object, row_seq: int) -> dict[str, object]:
        if type(raw) is not dict or set(raw) != _EVENT_KEYS:
            raise ConfirmationIntegrityError("confirmation event has unexpected fields")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
            raise ConfirmationIntegrityError("confirmation event schema version mismatch")
        if type(raw["seq"]) is not int or raw["seq"] != row_seq:
            raise ConfirmationIntegrityError("confirmation event sequence mismatch")
        for field in ("confirmation_id", "action_id", "risk_class", "ui_session_id", "interaction_id"):
            try:
                _validate_token(field, raw[field])
            except ConfirmationInputError as exc:
                raise ConfirmationIntegrityError(str(exc)) from exc
        try:
            _validate_sha256("payload_sha256", raw["payload_sha256"])
        except ConfirmationInputError as exc:
            raise ConfirmationIntegrityError(str(exc)) from exc
        if raw["event_type"] not in _EVENT_TYPES:
            raise ConfirmationIntegrityError("confirmation event_type is invalid")
        if raw["interaction_kind"] not in _INTERACTION_KINDS:
            raise ConfirmationIntegrityError("confirmation interaction_kind is invalid")
        opened_at = _parse_utc_text(raw["opened_at"], "opened_at")
        expires_at = _parse_utc_text(raw["expires_at"], "expires_at")
        event_at = _parse_utc_text(raw["event_at"], "event_at")
        if expires_at <= opened_at:
            raise ConfirmationIntegrityError("confirmation expiry must follow opening")
        if event_at < opened_at:
            raise ConfirmationIntegrityError("confirmation event precedes opening")
        event_type = raw["event_type"]
        if event_type == "OPENED":
            if raw["interaction_kind"] != "OPEN_GESTURE":
                raise ConfirmationIntegrityError("OPENED must use OPEN_GESTURE")
            try:
                _validate_operator_text(raw["operator_text"])
            except ConfirmationInputError as exc:
                raise ConfirmationIntegrityError(str(exc)) from exc
        else:
            if raw["operator_text"] is not None:
                raise ConfirmationIntegrityError("only OPENED may persist operator_text")
            expected_kind = (
                "DECISION_GESTURE" if event_type in {"APPROVED", "CANCELLED"} else "CONSUME_REQUEST"
            )
            if raw["interaction_kind"] != expected_kind:
                raise ConfirmationIntegrityError(
                    f"{event_type} must use {expected_kind}"
                )
        return raw

    def _load_audit(self, connection: sqlite3.Connection) -> _LoadedAudit:
        self._validate_schema(connection)
        rows = connection.execute(
            "SELECT seq, body_json, prev_sha256, event_sha256 "
            "FROM confirmation_events ORDER BY seq"
        ).fetchall()
        states: dict[str, _State] = {}
        used_interactions: set[str] = set()
        expected_prev = _ZERO_HASH

        for expected_seq, row in enumerate(rows, 1):
            seq, body_json, prev_sha256, event_sha256 = row
            if type(seq) is not int or seq != expected_seq:
                raise ConfirmationIntegrityError("confirmation audit sequence is not contiguous")
            if type(body_json) is not str:
                raise ConfirmationIntegrityError("confirmation audit body must be text")
            if type(prev_sha256) is not str or not _SHA256_RE.fullmatch(prev_sha256):
                raise ConfirmationIntegrityError("confirmation audit prev hash is invalid")
            if type(event_sha256) is not str or not _SHA256_RE.fullmatch(event_sha256):
                raise ConfirmationIntegrityError("confirmation audit event hash is invalid")
            if prev_sha256 != expected_prev:
                raise ConfirmationIntegrityError("confirmation audit hash chain is broken")
            if _hash_event(prev_sha256, body_json) != event_sha256:
                raise ConfirmationIntegrityError("confirmation audit event digest mismatch")
            try:
                decoded = strict_json_loads(body_json)
            except (TypeError, ValueError) as exc:
                raise ConfirmationIntegrityError("confirmation event JSON is invalid") from exc
            if _canonical_json(decoded) != body_json:
                raise ConfirmationIntegrityError("confirmation event JSON is not canonical")
            raw = self._validate_event_body(decoded, seq)
            interaction_id = str(raw["interaction_id"])
            if interaction_id in used_interactions:
                raise ConfirmationIntegrityError("confirmation interaction id was reused")
            used_interactions.add(interaction_id)

            confirmation_id = str(raw["confirmation_id"])
            event_type = str(raw["event_type"])
            if event_type == "OPENED":
                if confirmation_id in states:
                    raise ConfirmationIntegrityError("confirmation id was opened more than once")
                states[confirmation_id] = _State(
                    confirmation_id=confirmation_id,
                    action_id=str(raw["action_id"]),
                    risk_class=str(raw["risk_class"]),
                    payload_sha256=str(raw["payload_sha256"]),
                    ui_session_id=str(raw["ui_session_id"]),
                    opened_at=str(raw["opened_at"]),
                    expires_at=str(raw["expires_at"]),
                    operator_text=str(raw["operator_text"]),
                    status=ConfirmationStatus.PENDING,
                    open_gesture_id=interaction_id,
                )
            else:
                state = states.get(confirmation_id)
                if state is None:
                    raise ConfirmationIntegrityError("confirmation transition has no OPENED event")
                identity = (
                    str(raw["action_id"]),
                    str(raw["risk_class"]),
                    str(raw["payload_sha256"]),
                    str(raw["ui_session_id"]),
                    str(raw["opened_at"]),
                    str(raw["expires_at"]),
                )
                expected_identity = (
                    state.action_id,
                    state.risk_class,
                    state.payload_sha256,
                    state.ui_session_id,
                    state.opened_at,
                    state.expires_at,
                )
                if identity != expected_identity:
                    raise ConfirmationIntegrityError("confirmation transition identity changed")
                event_at = _parse_utc_text(raw["event_at"], "event_at")
                if event_at >= _parse_utc_text(state.expires_at, "expires_at"):
                    raise ConfirmationIntegrityError("confirmation transition occurred after expiry")
                if event_type in {"APPROVED", "CANCELLED"}:
                    if state.status is not ConfirmationStatus.PENDING:
                        raise ConfirmationIntegrityError("confirmation decision is not from PENDING")
                    if interaction_id == state.open_gesture_id:
                        raise ConfirmationIntegrityError(
                            "opening gesture cannot decide the confirmation"
                        )
                    if event_type == "APPROVED":
                        state.status = ConfirmationStatus.APPROVED
                        state.approval_grant_id = event_sha256
                        state.approved_at = str(raw["event_at"])
                    else:
                        state.status = ConfirmationStatus.CANCELLED
                elif event_type == "CONSUMED":
                    if state.status is not ConfirmationStatus.APPROVED:
                        raise ConfirmationIntegrityError("only APPROVED confirmation may be consumed")
                    state.status = ConfirmationStatus.CONSUMED
                    state.consumed_at = str(raw["event_at"])
            expected_prev = event_sha256

        return _LoadedAudit(
            states=states,
            used_interactions=frozenset(used_interactions),
            tail_sha256=expected_prev,
            event_count=len(rows),
        )

    def _event_body(
        self,
        *,
        seq: int,
        event_type: str,
        state: _State,
        interaction_kind: str,
        interaction_id: str,
        event_at: datetime,
        operator_text: str | None,
    ) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "seq": seq,
            "confirmation_id": state.confirmation_id,
            "event_type": event_type,
            "action_id": state.action_id,
            "risk_class": state.risk_class,
            "payload_sha256": state.payload_sha256,
            "ui_session_id": state.ui_session_id,
            "interaction_kind": interaction_kind,
            "interaction_id": interaction_id,
            "operator_text": operator_text,
            "opened_at": state.opened_at,
            "expires_at": state.expires_at,
            "event_at": _utc_text(event_at),
        }

    def _append_event(
        self,
        connection: sqlite3.Connection,
        audit: _LoadedAudit,
        *,
        event_type: str,
        state: _State,
        interaction_kind: str,
        interaction_id: str,
        event_at: datetime,
        operator_text: str | None = None,
    ) -> str:
        interaction_id = _validate_token("interaction_id", interaction_id)
        if interaction_id in audit.used_interactions:
            raise ConfirmationStateError("interaction_id has already been used")
        seq = audit.event_count + 1
        body = self._event_body(
            seq=seq,
            event_type=event_type,
            state=state,
            interaction_kind=interaction_kind,
            interaction_id=interaction_id,
            event_at=event_at,
            operator_text=operator_text,
        )
        body_json = _canonical_json(body)
        event_sha256 = _hash_event(audit.tail_sha256, body_json)
        connection.execute(
            "INSERT INTO confirmation_events(seq, body_json, prev_sha256, event_sha256) "
            "VALUES(?,?,?,?)",
            (seq, body_json, audit.tail_sha256, event_sha256),
        )
        return event_sha256

    def _identity_matches(
        self,
        state: _State,
        *,
        action_id: object,
        risk_class: object,
        payload_sha256: object,
    ) -> None:
        action = _validate_token("action_id", action_id)
        risk = _validate_token("risk_class", risk_class)
        payload = _validate_sha256("payload_sha256", payload_sha256)
        if (action, risk, payload) != (
            state.action_id,
            state.risk_class,
            state.payload_sha256,
        ):
            raise ConfirmationStateError("confirmation identity does not match the requested action")

    def _require_current_live_state(
        self,
        state: _State,
        *,
        now: datetime,
        expected_status: ConfirmationStatus,
    ) -> None:
        if state.ui_session_id != self.ui_session_id:
            raise ConfirmationStateError("confirmation belongs to a stale UI session")
        if now >= _parse_utc_text(state.expires_at, "expires_at"):
            raise ConfirmationStateError("confirmation has expired")
        if state.status is not expected_status:
            raise ConfirmationStateError(
                f"confirmation must be {expected_status.value}; current state is {state.status.value}"
            )

    @_serialized
    def open_confirmation(
        self,
        *,
        action_id: str,
        risk_class: str,
        payload_sha256: str,
        operator_text: str,
        open_gesture_id: str,
        ttl_seconds: int = 120,
    ) -> ConfirmationPrompt:
        action = _validate_token("action_id", action_id)
        risk = _validate_token("risk_class", risk_class)
        payload = _validate_sha256("payload_sha256", payload_sha256)
        presentation = _validate_operator_text(operator_text)
        gesture = _validate_token("open_gesture_id", open_gesture_id)
        ttl = _validate_ttl_seconds(ttl_seconds)
        now = self._now()
        expires = now + timedelta(seconds=ttl)
        confirmation_id = _validate_token(
            "confirmation_id", self._confirmation_id_factory()
        )
        state = _State(
            confirmation_id=confirmation_id,
            action_id=action,
            risk_class=risk,
            payload_sha256=payload,
            ui_session_id=self.ui_session_id,
            opened_at=_utc_text(now),
            expires_at=_utc_text(expires),
            operator_text=presentation,
            status=ConfirmationStatus.PENDING,
            open_gesture_id=gesture,
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            audit = self._current_audit(connection)
            if confirmation_id in audit.states:
                raise ConfirmationStateError("generated confirmation_id already exists")
            event_sha256 = self._append_event(
                connection,
                audit,
                event_type="OPENED",
                state=state,
                interaction_kind="OPEN_GESTURE",
                interaction_id=gesture,
                event_at=now,
                operator_text=presentation,
            )
            connection.commit()
            audit.states[confirmation_id] = state
            self._advance_cache(audit, interaction_id=gesture, event_sha256=event_sha256)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return ConfirmationPrompt(
            confirmation_id=confirmation_id,
            action_id=action,
            risk_class=risk,
            payload_sha256=payload,
            ui_session_id=self.ui_session_id,
            opened_at=state.opened_at,
            expires_at=state.expires_at,
            operator_text=presentation,
        )

    @_serialized
    def approve(
        self,
        confirmation_id: str,
        *,
        action_id: str,
        risk_class: str,
        payload_sha256: str,
        decision_gesture_id: str,
    ) -> AuthorizationGrant:
        confirmation_id = _validate_token("confirmation_id", confirmation_id)
        decision_gesture_id = _validate_token(
            "decision_gesture_id", decision_gesture_id
        )
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            audit = self._current_audit(connection)
            state = audit.states.get(confirmation_id)
            if state is None:
                raise ConfirmationStateError("unknown confirmation_id")
            self._identity_matches(
                state,
                action_id=action_id,
                risk_class=risk_class,
                payload_sha256=payload_sha256,
            )
            self._require_current_live_state(
                state, now=now, expected_status=ConfirmationStatus.PENDING
            )
            if decision_gesture_id == state.open_gesture_id:
                raise ConfirmationStateError(
                    "opening gesture cannot approve the confirmation"
                )
            grant_id = self._append_event(
                connection,
                audit,
                event_type="APPROVED",
                state=state,
                interaction_kind="DECISION_GESTURE",
                interaction_id=decision_gesture_id,
                event_at=now,
            )
            connection.commit()
            state.status = ConfirmationStatus.APPROVED
            state.approval_grant_id = grant_id
            state.approved_at = _utc_text(now)
            self._advance_cache(
                audit, interaction_id=decision_gesture_id, event_sha256=grant_id
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return AuthorizationGrant(
            confirmation_id=state.confirmation_id,
            grant_id=grant_id,
            action_id=state.action_id,
            risk_class=state.risk_class,
            payload_sha256=state.payload_sha256,
            ui_session_id=state.ui_session_id,
            approved_at=_utc_text(now),
            expires_at=state.expires_at,
        )

    @_serialized
    def cancel(
        self,
        confirmation_id: str,
        *,
        action_id: str,
        risk_class: str,
        payload_sha256: str,
        decision_gesture_id: str,
    ) -> ConfirmationView:
        confirmation_id = _validate_token("confirmation_id", confirmation_id)
        decision_gesture_id = _validate_token(
            "decision_gesture_id", decision_gesture_id
        )
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            audit = self._current_audit(connection)
            state = audit.states.get(confirmation_id)
            if state is None:
                raise ConfirmationStateError("unknown confirmation_id")
            self._identity_matches(
                state,
                action_id=action_id,
                risk_class=risk_class,
                payload_sha256=payload_sha256,
            )
            self._require_current_live_state(
                state, now=now, expected_status=ConfirmationStatus.PENDING
            )
            if decision_gesture_id == state.open_gesture_id:
                raise ConfirmationStateError(
                    "opening gesture cannot cancel the confirmation"
                )
            event_sha256 = self._append_event(
                connection,
                audit,
                event_type="CANCELLED",
                state=state,
                interaction_kind="DECISION_GESTURE",
                interaction_id=decision_gesture_id,
                event_at=now,
            )
            connection.commit()
            state.status = ConfirmationStatus.CANCELLED
            self._advance_cache(
                audit, interaction_id=decision_gesture_id, event_sha256=event_sha256
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.view(confirmation_id)

    @_serialized
    def consume(
        self,
        grant: AuthorizationGrant,
        *,
        action_id: str,
        risk_class: str,
        payload_sha256: str,
        consume_request_id: str,
    ) -> ConsumptionReceipt:
        if type(grant) is not AuthorizationGrant:
            raise ConfirmationInputError("grant must be an exact AuthorizationGrant")
        confirmation_id = _validate_token("confirmation_id", grant.confirmation_id)
        grant_id = _validate_sha256("grant_id", grant.grant_id)
        consume_request_id = _validate_token(
            "consume_request_id", consume_request_id
        )
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            audit = self._current_audit(connection)
            state = audit.states.get(confirmation_id)
            if state is None:
                raise ConfirmationStateError("unknown confirmation_id")
            self._identity_matches(
                state,
                action_id=action_id,
                risk_class=risk_class,
                payload_sha256=payload_sha256,
            )
            self._require_current_live_state(
                state, now=now, expected_status=ConfirmationStatus.APPROVED
            )
            if state.approval_grant_id != grant_id:
                raise ConfirmationStateError("authorization grant does not match persisted approval")
            grant_identity = (
                grant.action_id,
                grant.risk_class,
                grant.payload_sha256,
                grant.ui_session_id,
                grant.expires_at,
            )
            state_identity = (
                state.action_id,
                state.risk_class,
                state.payload_sha256,
                state.ui_session_id,
                state.expires_at,
            )
            if grant_identity != state_identity or grant.approved_at != state.approved_at:
                raise ConfirmationStateError("authorization grant fields were rebound")
            event_sha256 = self._append_event(
                connection,
                audit,
                event_type="CONSUMED",
                state=state,
                interaction_kind="CONSUME_REQUEST",
                interaction_id=consume_request_id,
                event_at=now,
            )
            connection.commit()
            state.status = ConfirmationStatus.CONSUMED
            state.consumed_at = _utc_text(now)
            self._advance_cache(
                audit, interaction_id=consume_request_id, event_sha256=event_sha256
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return ConsumptionReceipt(
            confirmation_id=state.confirmation_id,
            grant_id=grant_id,
            action_id=state.action_id,
            risk_class=state.risk_class,
            payload_sha256=state.payload_sha256,
            ui_session_id=state.ui_session_id,
            consumed_at=_utc_text(now),
            audit_event_sha256=event_sha256,
        )

    @_serialized
    def view(self, confirmation_id: str) -> ConfirmationView:
        confirmation_id = _validate_token("confirmation_id", confirmation_id)
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            audit = self._current_audit(connection)
            state = audit.states.get(confirmation_id)
            if state is None:
                raise ConfirmationStateError("unknown confirmation_id")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        status = state.status
        if status in {ConfirmationStatus.PENDING, ConfirmationStatus.APPROVED}:
            if state.ui_session_id != self.ui_session_id:
                status = ConfirmationStatus.STALE_SESSION
            elif now >= _parse_utc_text(state.expires_at, "expires_at"):
                status = ConfirmationStatus.EXPIRED
        return ConfirmationView(
            confirmation_id=state.confirmation_id,
            action_id=state.action_id,
            risk_class=state.risk_class,
            payload_sha256=state.payload_sha256,
            ui_session_id=state.ui_session_id,
            opened_at=state.opened_at,
            expires_at=state.expires_at,
            status=status,
            approval_grant_id=state.approval_grant_id,
        )

    @_serialized
    def audit_head(self) -> tuple[int, str]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            audit = self._current_audit(connection)
            connection.commit()
            return audit.event_count, audit.tail_sha256
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
