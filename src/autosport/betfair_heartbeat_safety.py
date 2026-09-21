"""Fail-closed Betfair Heartbeat safety evidence.

Provider-write free. A valid record is only a necessary safety precondition;
it never authorizes execution or real money. Stream heartbeats are out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

VERSION = 1
NS = 1_000_000_000


class BetfairHeartbeatSafetyError(RuntimeError):
    pass


class HeartbeatAction(str, Enum):
    NONE = "NONE"
    CANCELLATION_REQUEST_SUBMITTED = "CANCELLATION_REQUEST_SUBMITTED"
    ALL_BETS_CANCELLED = "ALL_BETS_CANCELLED"
    SOME_BETS_NOT_CANCELLED = "SOME_BETS_NOT_CANCELLED"
    CANCELLATION_REQUEST_ERROR = "CANCELLATION_REQUEST_ERROR"
    CANCELLATION_STATUS_UNKNOWN = "CANCELLATION_STATUS_UNKNOWN"


def _text(v, n):
    if type(v) is not str or not v or v != v.strip() or "\x00" in v:
        raise BetfairHeartbeatSafetyError(f"{n} must be canonical text")
    return v


def _sha(v, n):
    v = _text(v, n)
    if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
        raise BetfairHeartbeatSafetyError(f"{n} must be lowercase sha256")
    return v


def _time(v, n):
    v = _text(v, n)
    try:
        d = datetime.fromisoformat(v)
    except ValueError as e:
        raise BetfairHeartbeatSafetyError(f"{n} must be ISO-8601") from e
    if d.tzinfo is None or d.utcoffset() is None:
        raise BetfairHeartbeatSafetyError(f"{n} must be timezone-aware")
    d = d.astimezone(timezone.utc)
    if v != d.isoformat(timespec="microseconds"):
        raise BetfairHeartbeatSafetyError(f"{n} must be canonical UTC microseconds")
    return d


def _int(v, n, *, positive=False):
    if type(v) is not int or v < 0 or (positive and v == 0):
        raise BetfairHeartbeatSafetyError(
            f"{n} must be {'positive' if positive else 'nonnegative'} int"
        )
    return v


def _digest(v):
    return sha256(
        json.dumps(
            v, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class HeartbeatScope:
    account_id: str
    session_identity_sha256: str
    app_identity_sha256: str
    jurisdiction: str

    def __post_init__(self):
        _text(self.account_id, "account_id")
        _sha(self.session_identity_sha256, "session_identity_sha256")
        _sha(self.app_identity_sha256, "app_identity_sha256")
        _text(self.jurisdiction, "jurisdiction")

    @property
    def identity(self):
        return _digest(
            {
                "v": VERSION,
                "account": self.account_id,
                "session": self.session_identity_sha256,
                "app": self.app_identity_sha256,
                "jurisdiction": self.jurisdiction,
            }
        )


@dataclass(frozen=True, slots=True)
class HeartbeatAck:
    scope: HeartbeatScope
    requested_timeout_seconds: int
    actual_timeout_seconds: int
    action: HeartbeatAction
    received_at: str
    received_monotonic_ns: int
    response_sha256: str

    def __post_init__(self):
        if not isinstance(self.scope, HeartbeatScope):
            raise BetfairHeartbeatSafetyError("scope must be HeartbeatScope")
        _int(self.requested_timeout_seconds, "requested_timeout_seconds")
        _int(self.actual_timeout_seconds, "actual_timeout_seconds")
        _int(self.received_monotonic_ns, "received_monotonic_ns", positive=True)
        _time(self.received_at, "received_at")
        _sha(self.response_sha256, "response_sha256")
        if not isinstance(self.action, HeartbeatAction):
            raise BetfairHeartbeatSafetyError("action must be HeartbeatAction")
        if self.requested_timeout_seconds == 0:
            if self.actual_timeout_seconds != 0:
                raise BetfairHeartbeatSafetyError(
                    "unregister must return actual timeout 0"
                )
        elif self.actual_timeout_seconds == 0:
            raise BetfairHeartbeatSafetyError(
                "registered heartbeat needs actual timeout > 0"
            )

    def payload(self):
        return {
            "v": VERSION,
            "scope": self.scope.identity,
            "requested": self.requested_timeout_seconds,
            "actual": self.actual_timeout_seconds,
            "action": self.action.value,
            "received_at": self.received_at,
            "received_monotonic_ns": self.received_monotonic_ns,
            "response_sha256": self.response_sha256,
        }

    @property
    def evidence_id(self):
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class HeartbeatSafety:
    scope_sha256: str
    evidence_id: str | None
    heartbeat_precondition: bool
    requires_reconciliation: bool
    next_due_at: str | None
    next_due_monotonic_ns: int | None
    expires_at: str | None
    reason: str

    @property
    def provider_write_authorized(self):
        return False

    @property
    def execution_admission_authorized(self):
        return False

    @property
    def real_money_authorized(self):
        return False


def evaluate(
    scope: HeartbeatScope,
    ack: HeartbeatAck | None,
    *,
    now_at: str,
    now_monotonic_ns: int,
):
    if not isinstance(scope, HeartbeatScope):
        raise BetfairHeartbeatSafetyError("scope must be HeartbeatScope")
    now = _time(now_at, "now_at")
    mono = _int(now_monotonic_ns, "now_monotonic_ns", positive=True)
    if ack is None:
        return HeartbeatSafety(
            scope.identity,
            None,
            False,
            True,
            None,
            None,
            None,
            "NO_ACK",
        )
    if not isinstance(ack, HeartbeatAck) or ack.scope != scope:
        raise BetfairHeartbeatSafetyError("ack scope/type mismatch")
    received = _time(ack.received_at, "received_at")
    if now < received:
        return HeartbeatSafety(
            scope.identity,
            ack.evidence_id,
            False,
            True,
            None,
            None,
            None,
            "WALL_CLOCK_REGRESSION",
        )
    if mono < ack.received_monotonic_ns:
        return HeartbeatSafety(
            scope.identity,
            ack.evidence_id,
            False,
            True,
            None,
            None,
            None,
            "MONOTONIC_EPOCH_CHANGED",
        )
    if ack.actual_timeout_seconds == 0:
        return HeartbeatSafety(
            scope.identity,
            ack.evidence_id,
            False,
            True,
            None,
            None,
            None,
            "UNREGISTERED",
        )

    timeout = ack.actual_timeout_seconds
    due = received + timedelta(seconds=timeout * 2 / 3)
    expiry = received + timedelta(seconds=timeout)
    due_ns = ack.received_monotonic_ns + timeout * NS * 2 // 3
    expiry_ns = ack.received_monotonic_ns + timeout * NS
    due_at = due.isoformat(timespec="microseconds")
    expiry_at = expiry.isoformat(timespec="microseconds")

    if ack.action is not HeartbeatAction.NONE:
        return HeartbeatSafety(
            scope.identity,
            ack.evidence_id,
            False,
            True,
            due_at,
            due_ns,
            expiry_at,
            f"DEADMAN_ACTION_{ack.action.value}",
        )
    fresh = now < expiry and mono < expiry_ns
    before_due = now < due and mono < due_ns
    safe = fresh and before_due
    reason = (
        "SAFE_PRECONDITION"
        if safe
        else ("SAFETY_MARGIN_EXHAUSTED" if fresh else "TIMEOUT_EXPIRED")
    )
    return HeartbeatSafety(
        scope.identity,
        ack.evidence_id,
        safe,
        not safe,
        due_at,
        due_ns,
        expiry_at,
        reason,
    )


class HeartbeatStore:
    FILE_NAME = "betfair_heartbeat_safety.sqlite3"

    def __init__(self, workspace: str | Path, *, scope: HeartbeatScope):
        self.scope = scope
        self.path = Path(workspace) / self.FILE_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS meta("
                "id INTEGER PRIMARY KEY CHECK(id=1), scope TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ack("
                "seq INTEGER PRIMARY KEY, evidence TEXT UNIQUE NOT NULL, "
                "prev TEXT, digest TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)"
            )
            row = db.execute(
                "SELECT scope FROM meta WHERE id=1"
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO meta VALUES(1,?)",
                    (scope.identity,),
                )
            elif row[0] != scope.identity:
                raise BetfairHeartbeatSafetyError("store scope mismatch")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _rows(self, db):
        rows = db.execute(
            "SELECT seq,evidence,prev,digest,payload FROM ack ORDER BY seq"
        ).fetchall()
        out = []
        prev = None
        prior = None
        for expected, row in enumerate(rows, 1):
            seq, evidence, predecessor, digest, raw = row
            if seq != expected or predecessor != prev:
                raise BetfairHeartbeatSafetyError("hash chain mismatch")
            data = json.loads(raw)
            if set(data) != {
                "v",
                "scope",
                "requested",
                "actual",
                "action",
                "received_at",
                "received_monotonic_ns",
                "response_sha256",
            }:
                raise BetfairHeartbeatSafetyError("noncanonical stored ack")
            try:
                action = HeartbeatAction(data["action"])
            except ValueError as e:
                raise BetfairHeartbeatSafetyError(
                    "invalid stored action"
                ) from e
            ack = HeartbeatAck(
                self.scope,
                data["requested"],
                data["actual"],
                action,
                data["received_at"],
                data["received_monotonic_ns"],
                data["response_sha256"],
            )
            if data["scope"] != self.scope.identity or evidence != ack.evidence_id:
                raise BetfairHeartbeatSafetyError("evidence identity mismatch")
            expected_digest = _digest(
                {
                    "seq": seq,
                    "evidence": evidence,
                    "prev": predecessor,
                    "payload": raw,
                }
            )
            if digest != expected_digest:
                raise BetfairHeartbeatSafetyError("record digest mismatch")
            if prior and (
                _time(ack.received_at, "received_at")
                <= _time(prior.received_at, "received_at")
                or ack.received_monotonic_ns
                <= prior.received_monotonic_ns
            ):
                raise BetfairHeartbeatSafetyError("ack time regression")
            out.append((row, ack))
            prev = digest
            prior = ack
        return out

    def record(self, ack: HeartbeatAck):
        if not isinstance(ack, HeartbeatAck) or ack.scope != self.scope:
            raise BetfairHeartbeatSafetyError("ack scope/type mismatch")
        raw = json.dumps(
            ack.payload(), sort_keys=True, separators=(",", ":")
        )
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._rows(db)
            if rows and rows[-1][1].evidence_id == ack.evidence_id:
                db.commit()
                return ack.evidence_id
            if rows:
                prior = rows[-1][1]
                if (
                    _time(ack.received_at, "received_at")
                    <= _time(prior.received_at, "received_at")
                    or ack.received_monotonic_ns
                    <= prior.received_monotonic_ns
                ):
                    raise BetfairHeartbeatSafetyError(
                        "new ack must advance both clocks"
                    )
            seq = len(rows) + 1
            prev = rows[-1][0][3] if rows else None
            digest = _digest(
                {
                    "seq": seq,
                    "evidence": ack.evidence_id,
                    "prev": prev,
                    "payload": raw,
                }
            )
            db.execute(
                "INSERT INTO ack VALUES(?,?,?,?,?)",
                (seq, ack.evidence_id, prev, digest, raw),
            )
            db.commit()
        return ack.evidence_id

    def latest(self):
        with self._db() as db:
            rows = self._rows(db)
        return rows[-1][1] if rows else None

    def verify_integrity(self):
        with self._db() as db:
            return len(self._rows(db))

    def evaluate(self, *, now_at: str, now_monotonic_ns: int):
        return evaluate(
            self.scope,
            self.latest(),
            now_at=now_at,
            now_monotonic_ns=now_monotonic_ns,
        )
