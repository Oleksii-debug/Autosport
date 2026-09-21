from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA_VERSION = 1
GENESIS_SHA256 = "0" * 64
REDACTED = "[REDACTED]"

_EVENT_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_SENSITIVE_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
}

_LIFECYCLE_STARTUP = "lifecycle.startup"
_LIFECYCLE_HEARTBEAT = "lifecycle.heartbeat"
_LIFECYCLE_SHUTDOWN = "lifecycle.shutdown"
_LIFECYCLE_UNCLEAN_RESTART = "lifecycle.unclean_restart"


class JournalError(RuntimeError):
    """Base class for forensic-session-journal failures."""


class JournalIntegrityError(JournalError):
    """Raised when an existing journal cannot be trusted exactly as stored."""


class JournalClosedError(JournalError):
    """Raised when a caller tries to append after close()."""


class JournalUncertainError(JournalError):
    """Raised after an append had an uncertain durability outcome."""


@dataclass(frozen=True, slots=True)
class JournalRecord:
    schema_version: int
    seq: int
    timestamp_utc: str
    session_id: str
    event_type: str
    payload: Mapping[str, Any]
    prev_sha256: str
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "seq": self.seq,
            "timestamp_utc": self.timestamp_utc,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "prev_sha256": self.prev_sha256,
            "sha256": self.sha256,
        }


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("journal clock must return a timezone-aware datetime")
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise JournalIntegrityError("journal timestamp is not canonical UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise JournalIntegrityError("journal timestamp is invalid") from exc
    # strptime accepts some non-canonical edge cases; round-trip closes that gap.
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise JournalIntegrityError("journal timestamp is not canonical UTC")
    return value


def _validate_event_type(value: Any) -> str:
    if not isinstance(value, str) or not _EVENT_RE.fullmatch(value):
        raise ValueError(f"invalid journal event_type: {value!r}")
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SENSITIVE_KEY_PARTS | {"api_key", "apikey", "session_key"}:
        return True
    parts = [part for part in normalized.split("_") if part]
    if parts and parts[-1] in _SENSITIVE_KEY_PARTS:
        return True
    if len(parts) >= 2 and parts[-2:] in (["api", "key"], ["session", "key"]):
        return True
    return False


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("journal payload mapping keys must be strings")
            output[raw_key] = _redact(child, key=raw_key)
        return output
    if isinstance(value, (list, tuple)):
        return [_redact(child) for child in value]
    # Fail closed instead of silently serializing repr() that can expose secrets.
    raise TypeError(f"journal payload type is not JSON-safe: {type(value).__name__}")


def redact_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise TypeError("journal payload must be a mapping")
    return _redact(payload)


def _assert_redaction_invariant(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_sensitive_key(key) and child != REDACTED:
                raise JournalIntegrityError("journal contains unredacted sensitive payload data")
            _assert_redaction_invariant(child)
    elif isinstance(value, list):
        for child in value:
            _assert_redaction_invariant(child)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _record_digest(unsigned: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise JournalIntegrityError(f"journal contains non-standard JSON constant: {value}")


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise JournalIntegrityError(f"journal contains duplicate JSON object key: {key!r}")
        output[key] = value
    return output


def _parse_line(line: str, *, expected_seq: int, expected_prev: str) -> JournalRecord:
    try:
        raw = json.loads(
            line,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise JournalIntegrityError("journal line is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise JournalIntegrityError("journal record must be a JSON object")
    try:
        canonical_line = _canonical_json(raw).decode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JournalIntegrityError("journal record is not canonical JSON") from exc
    if line != canonical_line:
        raise JournalIntegrityError("journal record is not canonical JSON")

    expected_keys = {
        "schema_version",
        "seq",
        "timestamp_utc",
        "session_id",
        "event_type",
        "payload",
        "prev_sha256",
        "sha256",
    }
    if set(raw) != expected_keys:
        raise JournalIntegrityError("journal record schema drift detected")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise JournalIntegrityError("unsupported journal schema version")
    if type(raw["seq"]) is not int or raw["seq"] != expected_seq:
        raise JournalIntegrityError("journal sequence is not contiguous")

    timestamp = _validate_timestamp(raw["timestamp_utc"])
    try:
        session_id = str(uuid.UUID(raw["session_id"]))
    except (ValueError, AttributeError, TypeError) as exc:
        raise JournalIntegrityError("journal session_id is invalid") from exc
    if session_id != raw["session_id"]:
        raise JournalIntegrityError("journal session_id is not canonical")

    event_type = raw["event_type"]
    try:
        _validate_event_type(event_type)
    except ValueError as exc:
        raise JournalIntegrityError("journal event_type is invalid") from exc

    payload = raw["payload"]
    if not isinstance(payload, dict):
        raise JournalIntegrityError("journal payload must be an object")
    _assert_redaction_invariant(payload)

    prev_sha256 = raw["prev_sha256"]
    sha256 = raw["sha256"]
    if not isinstance(prev_sha256, str) or not _SHA256_RE.fullmatch(prev_sha256):
        raise JournalIntegrityError("journal prev_sha256 is invalid")
    if prev_sha256 != expected_prev:
        raise JournalIntegrityError("journal hash-chain predecessor mismatch")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise JournalIntegrityError("journal sha256 is invalid")

    unsigned = dict(raw)
    del unsigned["sha256"]
    expected_sha = _record_digest(unsigned)
    if sha256 != expected_sha:
        raise JournalIntegrityError("journal record hash mismatch")

    return JournalRecord(
        schema_version=SCHEMA_VERSION,
        seq=expected_seq,
        timestamp_utc=timestamp,
        session_id=session_id,
        event_type=event_type,
        payload=payload,
        prev_sha256=prev_sha256,
        sha256=sha256,
    )


def read_verified_records(path: str | os.PathLike[str]) -> tuple[JournalRecord, ...]:
    journal_path = Path(path)
    if not journal_path.exists():
        return ()
    raw = journal_path.read_bytes()
    if not raw:
        return ()
    if not raw.endswith(b"\n"):
        raise JournalIntegrityError("journal has a torn or unterminated tail")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise JournalIntegrityError("journal is not valid UTF-8") from exc

    records: list[JournalRecord] = []
    expected_prev = GENESIS_SHA256
    for expected_seq, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise JournalIntegrityError("journal contains an empty record")
        record = _parse_line(line, expected_seq=expected_seq, expected_prev=expected_prev)
        records.append(record)
        expected_prev = record.sha256
    return tuple(records)


def verify_journal(path: str | os.PathLike[str]) -> tuple[JournalRecord, ...]:
    """Verify the complete journal and return immutable records if trusted.

    The SHA-256 chain detects corruption and edits unless an actor can rewrite and
    re-hash the chain. It is an integrity aid, not an authenticity mechanism; no
    MAC/signature or externally anchored head digest is implied.
    """
    return read_verified_records(path)


class ForensicSessionJournal:
    """Durable append-only lifecycle journal with strict integrity verification.

    This journal is observational evidence only. It must not be used as a source of
    economic, decision, execution-authority, settlement, or run truth.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Clock = _utc_now,
        session_id: str | None = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._uncertain = False
        self._records = list(read_verified_records(self._path))
        self._seq = len(self._records)
        self._last_sha256 = self._records[-1].sha256 if self._records else GENESIS_SHA256

        if session_id is None:
            self._session_id = str(uuid.uuid4())
        else:
            parsed = str(uuid.UUID(session_id))
            if parsed != session_id:
                raise ValueError("session_id must be canonical UUID text")
            self._session_id = parsed

        prior = self._records[-1] if self._records else None
        if prior is not None and prior.event_type != _LIFECYCLE_SHUTDOWN:
            self._append_locked(
                _LIFECYCLE_UNCLEAN_RESTART,
                {
                    "prior_seq": prior.seq,
                    "prior_session_id": prior.session_id,
                    "prior_event_type": prior.event_type,
                    "prior_sha256": prior.sha256,
                },
            )
        self._append_locked(_LIFECYCLE_STARTUP, {})

    @property
    def path(self) -> Path:
        return self._path

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _append_locked(self, event_type: str, payload: Mapping[str, Any] | None) -> JournalRecord:
        if self._uncertain:
            raise JournalUncertainError(
                "journal append durability is uncertain; reopen and reverify before continuing"
            )
        if self._closed:
            raise JournalClosedError("forensic session journal is closed")
        event_type = _validate_event_type(event_type)
        sanitized = redact_payload(payload)
        seq = self._seq + 1
        unsigned = {
            "schema_version": SCHEMA_VERSION,
            "seq": seq,
            "timestamp_utc": _canonical_timestamp(self._clock()),
            "session_id": self._session_id,
            "event_type": event_type,
            "payload": sanitized,
            "prev_sha256": self._last_sha256,
        }
        digest = _record_digest(unsigned)
        serialized = _canonical_json({**unsigned, "sha256": digest}) + b"\n"

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        fd: int | None = None
        try:
            fd = os.open(self._path, flags, 0o600)
            view = memoryview(serialized)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("journal append made no progress")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
        except OSError as exc:
            self._uncertain = True
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise JournalUncertainError(
                "journal append durability is uncertain; reopen and reverify before continuing"
            ) from exc

        record = JournalRecord(
            schema_version=SCHEMA_VERSION,
            seq=seq,
            timestamp_utc=unsigned["timestamp_utc"],
            session_id=self._session_id,
            event_type=event_type,
            payload=sanitized,
            prev_sha256=self._last_sha256,
            sha256=digest,
        )
        self._seq = seq
        self._last_sha256 = digest
        self._records.append(record)
        return record

    def append_material(
        self, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> JournalRecord:
        if event_type.startswith("lifecycle."):
            raise ValueError("lifecycle.* event types are journal-owned")
        with self._lock:
            return self._append_locked(event_type, payload)

    def heartbeat(self, payload: Mapping[str, Any] | None = None) -> JournalRecord:
        with self._lock:
            return self._append_locked(_LIFECYCLE_HEARTBEAT, payload)

    def snapshot(self) -> tuple[JournalRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def close(self, payload: Mapping[str, Any] | None = None) -> JournalRecord | None:
        with self._lock:
            if self._closed:
                return None
            record = self._append_locked(_LIFECYCLE_SHUTDOWN, payload)
            self._closed = True
            return record

    def __enter__(self) -> "ForensicSessionJournal":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close({"exit": "error" if exc_type is not None else "clean"})
        return False
