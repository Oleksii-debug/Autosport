from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable

SCHEMA_VERSION = "1"
MAX_KEY_CHARS = 256
MAX_MESSAGE_CHARS = 4096


class OperatorStatusBrokerError(RuntimeError):
    pass


class OperatorStatusIntegrityError(OperatorStatusBrokerError):
    pass


class OperatorStatusConflictError(OperatorStatusBrokerError):
    pass


class OperatorStatusNotPendingError(OperatorStatusBrokerError):
    pass


class AnnouncementPriority(StrEnum):
    SILENT = "SILENT"
    POLITE = "POLITE"
    ASSERTIVE = "ASSERTIVE"


@dataclass(frozen=True, slots=True)
class AnnouncementRecord:
    sequence: int
    event_id: str
    idempotency_key: str
    priority: AnnouncementPriority
    coalesce_key: str | None
    message: str
    occurred_at: datetime
    published_at: datetime
    superseded_by: str | None
    acknowledged_at: datetime | None
    record_sha256: str


@dataclass(frozen=True, slots=True)
class AnnouncementPresentation:
    event_id: str
    message: str
    live_mode: str
    role: str
    atomic: bool = True
    request_focus: bool = False
    focus_target: None = None


def _text(value: object, name: str, maximum: int) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return _utc(value, "instant").isoformat().replace("+00:00", "Z")


def _parse_time(value: object, name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise OperatorStatusIntegrityError(f"{name} is not canonical UTC text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise OperatorStatusIntegrityError(f"{name} is invalid") from exc
    if _time_text(parsed) != value:
        raise OperatorStatusIntegrityError(f"{name} is not canonical UTC text")
    return parsed


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _logical_payload(
    key: str,
    priority: AnnouncementPriority,
    coalesce_key: str | None,
    message: str,
    occurred_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "idempotency_key": key,
        "priority": priority.value,
        "coalesce_key": coalesce_key,
        "message": message,
        "occurred_at": _time_text(occurred_at),
    }


def _state_payload(record: AnnouncementRecord) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sequence": record.sequence,
        "event_id": record.event_id,
        "idempotency_key": record.idempotency_key,
        "priority": record.priority.value,
        "coalesce_key": record.coalesce_key,
        "message": record.message,
        "occurred_at": _time_text(record.occurred_at),
        "published_at": _time_text(record.published_at),
        "superseded_by": record.superseded_by,
        "acknowledged_at": (
            None if record.acknowledged_at is None else _time_text(record.acknowledged_at)
        ),
    }


class OperatorStatusAnnouncementBroker:
    """Durable presentation-only queue for screen-reader/operator announcements."""

    _COLUMNS = (
        "sequence", "event_id", "idempotency_key", "priority", "coalesce_key",
        "message", "occurred_at", "published_at", "superseded_by",
        "acknowledged_at", "record_sha256",
    )
    _EVENT_SCHEMA = (
        ("sequence", "INTEGER", 0, 1),
        ("event_id", "TEXT", 1, 0),
        ("idempotency_key", "TEXT", 1, 0),
        ("priority", "TEXT", 1, 0),
        ("coalesce_key", "TEXT", 0, 0),
        ("message", "TEXT", 1, 0),
        ("occurred_at", "TEXT", 1, 0),
        ("published_at", "TEXT", 1, 0),
        ("superseded_by", "TEXT", 0, 0),
        ("acknowledged_at", "TEXT", 0, 0),
        ("record_sha256", "TEXT", 1, 0),
    )
    _METADATA_SCHEMA = (
        ("key", "TEXT", 0, 1),
        ("value", "TEXT", 1, 0),
    )

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        sqlite_timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(sqlite_timeout_seconds, bool) or not isinstance(
            sqlite_timeout_seconds, (int, float)
        ):
            raise ValueError("sqlite_timeout_seconds must be a finite positive number")
        if not math.isfinite(float(sqlite_timeout_seconds)) or sqlite_timeout_seconds <= 0:
            raise ValueError("sqlite_timeout_seconds must be a finite positive number")
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                self.path, timeout=float(sqlite_timeout_seconds), isolation_level=None
            )
            self._db.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
            self.verify_integrity()
        except OperatorStatusIntegrityError:
            db = getattr(self, "_db", None)
            if db is not None:
                db.close()
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            db = getattr(self, "_db", None)
            if db is not None:
                db.close()
            raise OperatorStatusIntegrityError(
                f"operator announcement store is unavailable: {type(exc).__name__}"
            ) from exc

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "OperatorStatusAnnouncementBroker":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        tables = {
            row[0] for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not tables:
            self._db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE events(
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    priority TEXT NOT NULL,
                    coalesce_key TEXT,
                    message TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    superseded_by TEXT,
                    acknowledged_at TEXT,
                    record_sha256 TEXT NOT NULL
                );
                INSERT INTO metadata VALUES('schema_version','1');
                COMMIT;
                """
            )
            tables = {"metadata", "events"}
        if tables != {"metadata", "events"}:
            raise OperatorStatusIntegrityError("operator announcement store table set mismatch")
        metadata = self._db.execute("SELECT key,value FROM metadata ORDER BY key").fetchall()
        if metadata != [("schema_version", SCHEMA_VERSION)]:
            raise OperatorStatusIntegrityError("operator announcement schema version mismatch")
        event_schema = tuple(
            (row[1], str(row[2]).upper(), row[3], row[5])
            for row in self._db.execute("PRAGMA table_info(events)")
        )
        metadata_schema = tuple(
            (row[1], str(row[2]).upper(), row[3], row[5])
            for row in self._db.execute("PRAGMA table_info(metadata)")
        )
        if event_schema != self._EVENT_SCHEMA or metadata_schema != self._METADATA_SCHEMA:
            raise OperatorStatusIntegrityError("operator announcement schema mismatch")

    def _now(self) -> datetime:
        return _utc(self._clock(), "clock result")

    @staticmethod
    def _coalesce(priority: AnnouncementPriority, value: object) -> str | None:
        if type(priority) is not AnnouncementPriority:
            raise ValueError("priority must be an exact AnnouncementPriority")
        if priority is AnnouncementPriority.POLITE:
            return _text(value, "coalesce_key", MAX_KEY_CHARS)
        if value is not None:
            raise ValueError("coalesce_key is supported only for POLITE announcements")
        return None

    def _record(self, row: tuple[object, ...]) -> AnnouncementRecord:
        try:
            sequence = row[0]
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise ValueError("sequence")
            event_id = _text(row[1], "event_id", 64)
            if len(event_id) != 64 or any(c not in "0123456789abcdef" for c in event_id):
                raise ValueError("event_id")
            key = _text(row[2], "idempotency_key", MAX_KEY_CHARS)
            priority = AnnouncementPriority(row[3])
            coalesce = self._coalesce(priority, row[4])
            message = _text(row[5], "message", MAX_MESSAGE_CHARS)
        except (TypeError, ValueError) as exc:
            raise OperatorStatusIntegrityError("malformed durable announcement row") from exc
        occurred = _parse_time(row[6], "occurred_at")
        published = _parse_time(row[7], "published_at")
        superseded = row[8]
        acknowledged = None if row[9] is None else _parse_time(row[9], "acknowledged_at")
        sha = row[10]
        if superseded is not None:
            try:
                superseded = _text(superseded, "superseded_by", 64)
            except ValueError as exc:
                raise OperatorStatusIntegrityError("malformed supersession identity") from exc
        logical = _digest(_logical_payload(key, priority, coalesce, message, occurred))
        provisional = AnnouncementRecord(
            sequence, event_id, key, priority, coalesce, message, occurred, published,
            superseded, acknowledged, str(sha),
        )
        if event_id != logical or occurred > published:
            raise OperatorStatusIntegrityError("announcement identity/chronology mismatch")
        if acknowledged is not None and acknowledged < published:
            raise OperatorStatusIntegrityError("acknowledgement predates publication")
        if priority is AnnouncementPriority.SILENT and (acknowledged or superseded):
            raise OperatorStatusIntegrityError("SILENT announcement has delivery state")
        if acknowledged is not None and superseded is not None:
            raise OperatorStatusIntegrityError("announcement is both acknowledged and superseded")
        if type(sha) is not str or sha != _digest(_state_payload(provisional)):
            raise OperatorStatusIntegrityError("announcement state digest mismatch")
        return provisional

    def _fetch(self, where: str, value: object) -> AnnouncementRecord | None:
        row = self._db.execute(
            f"SELECT {','.join(self._COLUMNS)} FROM events WHERE {where}=?", (value,)
        ).fetchone()
        return None if row is None else self._record(row)

    def _write_state(self, record: AnnouncementRecord) -> None:
        updated = AnnouncementRecord(
            record.sequence, record.event_id, record.idempotency_key, record.priority,
            record.coalesce_key, record.message, record.occurred_at, record.published_at,
            record.superseded_by, record.acknowledged_at, "",
        )
        sha = _digest(_state_payload(updated))
        self._db.execute(
            "UPDATE events SET superseded_by=?,acknowledged_at=?,record_sha256=? WHERE event_id=?",
            (
                updated.superseded_by,
                None if updated.acknowledged_at is None else _time_text(updated.acknowledged_at),
                sha,
                updated.event_id,
            ),
        )

    def publish(
        self,
        *,
        idempotency_key: str,
        priority: AnnouncementPriority,
        message: str,
        coalesce_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> AnnouncementRecord:
        key = _text(idempotency_key, "idempotency_key", MAX_KEY_CHARS)
        coalesce = self._coalesce(priority, coalesce_key)
        message = _text(message, "message", MAX_MESSAGE_CHARS)
        occurred = None if occurred_at is None else _utc(occurred_at, "occurred_at")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            prior = self._fetch("idempotency_key", key)
            if prior is not None:
                candidate_time = prior.occurred_at if occurred is None else occurred
                candidate_id = _digest(
                    _logical_payload(key, priority, coalesce, message, candidate_time)
                )
                if candidate_id != prior.event_id:
                    raise OperatorStatusConflictError(
                        "idempotency key is already bound to different content"
                    )
                self._db.execute("COMMIT")
                return prior
            published = self._now()
            occurred = published if occurred is None else occurred
            if occurred > published:
                raise ValueError("occurred_at must not be later than publication time")
            event_id = _digest(_logical_payload(key, priority, coalesce, message, occurred))
            sequence = self._db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM events"
            ).fetchone()[0]
            record = AnnouncementRecord(
                sequence, event_id, key, priority, coalesce, message, occurred,
                published, None, None, "",
            )
            sha = _digest(_state_payload(record))
            self._db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sequence, event_id, key, priority.value, coalesce, message,
                    _time_text(occurred), _time_text(published), None, None, sha,
                ),
            )
            if priority is AnnouncementPriority.POLITE:
                rows = self._db.execute(
                    f"""SELECT {','.join(self._COLUMNS)} FROM events
                        WHERE priority='POLITE' AND coalesce_key=? AND event_id<>?
                          AND superseded_by IS NULL AND acknowledged_at IS NULL
                        ORDER BY sequence""",
                    (coalesce, event_id),
                ).fetchall()
                for row in rows:
                    old = self._record(row)
                    self._write_state(
                        AnnouncementRecord(
                            old.sequence, old.event_id, old.idempotency_key, old.priority,
                            old.coalesce_key, old.message, old.occurred_at, old.published_at,
                            event_id, None, "",
                        )
                    )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        result = self._fetch("event_id", event_id)
        if result is None:
            raise OperatorStatusIntegrityError("published announcement disappeared")
        return result

    def pending(self, *, limit: int = 100) -> tuple[AnnouncementRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        rows = self._db.execute(
            f"""SELECT {','.join(self._COLUMNS)} FROM events
                WHERE priority<>'SILENT' AND superseded_by IS NULL
                  AND acknowledged_at IS NULL ORDER BY sequence LIMIT ?""",
            (limit,),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    @staticmethod
    def presentation_for(record: AnnouncementRecord) -> AnnouncementPresentation:
        if type(record) is not AnnouncementRecord or record.priority is AnnouncementPriority.SILENT:
            raise ValueError("record must be a presentable AnnouncementRecord")
        return AnnouncementPresentation(
            event_id=record.event_id,
            message=record.message,
            live_mode="polite" if record.priority is AnnouncementPriority.POLITE else "assertive",
            role="status" if record.priority is AnnouncementPriority.POLITE else "alert",
        )

    def pending_presentations(self, *, limit: int = 100) -> tuple[AnnouncementPresentation, ...]:
        return tuple(self.presentation_for(record) for record in self.pending(limit=limit))

    def acknowledge(self, event_id: str) -> AnnouncementRecord:
        event_id = _text(event_id, "event_id", 64)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            record = self._fetch("event_id", event_id)
            if record is None:
                raise OperatorStatusNotPendingError("announcement does not exist")
            if record.acknowledged_at is not None:
                self._db.execute("COMMIT")
                return record
            if record.priority is AnnouncementPriority.SILENT or record.superseded_by is not None:
                raise OperatorStatusNotPendingError("announcement is not pending delivery")
            acknowledged_at = self._now()
            if acknowledged_at < record.published_at:
                raise OperatorStatusIntegrityError(
                    "acknowledgement clock predates publication"
                )
            self._write_state(
                AnnouncementRecord(
                    record.sequence, record.event_id, record.idempotency_key, record.priority,
                    record.coalesce_key, record.message, record.occurred_at, record.published_at,
                    None, acknowledged_at, "",
                )
            )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        result = self._fetch("event_id", event_id)
        if result is None:
            raise OperatorStatusIntegrityError("acknowledged announcement disappeared")
        return result

    def verify_integrity(self) -> None:
        rows = self._db.execute(
            f"SELECT {','.join(self._COLUMNS)} FROM events ORDER BY sequence"
        ).fetchall()
        records = [self._record(row) for row in rows]
        if [record.sequence for record in records] != list(range(1, len(records) + 1)):
            raise OperatorStatusIntegrityError("announcement sequence is not contiguous")
        by_id = {record.event_id: record for record in records}
        for record in records:
            if record.superseded_by is None:
                continue
            replacement = by_id.get(record.superseded_by)
            if replacement is None:
                raise OperatorStatusIntegrityError("supersession references unknown event")
            if (
                record.priority is not AnnouncementPriority.POLITE
                or replacement.priority is not AnnouncementPriority.POLITE
                or record.coalesce_key != replacement.coalesce_key
                or replacement.sequence <= record.sequence
            ):
                raise OperatorStatusIntegrityError("invalid polite supersession chain")
