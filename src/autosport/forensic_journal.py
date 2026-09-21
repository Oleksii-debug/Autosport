from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Callable, Mapping, Sequence, TypeAlias

from .integrity import atomic_write_json, durable_path_lock, ensure_durable_file


_SCHEMA_VERSION = 1
_GENESIS_SHA256 = "0" * 64
_REDACTED = "[REDACTED]"
_HEX_DIGITS = frozenset("0123456789abcdef")
_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "sequence",
        "occurred_at",
        "kind",
        "source",
        "heartbeat_state",
        "event_name",
        "message",
        "details",
        "previous_sha256",
        "record_sha256",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {"schema_version", "record_count", "last_record_sha256"}
)
_SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credential",
    "api_key",
    "apikey",
    "private_key",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie)"
    r"\s*([:=])\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URI_CREDENTIAL_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@"
)


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ForensicJournalError(RuntimeError):
    """Base failure for the durable forensic session journal."""


class ForensicJournalIntegrityError(ForensicJournalError):
    """Raised when durable journal/checkpoint evidence is malformed or inconsistent."""


class ForensicEventKind(str, Enum):
    STARTUP = "STARTUP"
    SHUTDOWN = "SHUTDOWN"
    CRASH = "CRASH"
    MATERIAL_EVENT = "MATERIAL_EVENT"
    HEARTBEAT = "HEARTBEAT"


class HeartbeatState(str, Enum):
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    IDLE = "IDLE"


@dataclass(frozen=True, slots=True)
class ForensicJournalRecord:
    sequence: int
    occurred_at: str
    kind: ForensicEventKind
    source: str
    event_name: str
    heartbeat_state: HeartbeatState | None
    message: str | None
    details: Mapping[str, JsonValue]
    previous_sha256: str
    record_sha256: str


@dataclass(frozen=True, slots=True)
class ForensicJournalIntegrity:
    record_count: int
    last_record_sha256: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ForensicJournalIntegrityError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ForensicJournalIntegrityError(f"non-finite JSON constant: {value}")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("forensic journal payload must be canonical JSON") from exc
    return text.encode("utf-8")


def _record_digest(payload_without_digest: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload_without_digest)).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("forensic journal timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_nonempty_text(label: str, value: object, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{label} is too long")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    return value


def _is_secret_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact_text(value: str) -> str:
    value = _URI_CREDENTIAL_RE.sub(r"\1[REDACTED]:[REDACTED]@", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    return _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
        value,
    )


def _sanitize_json_value(value: object, *, secret_context: bool = False) -> JsonValue:
    if secret_context:
        return _REDACTED
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("forensic journal details must not contain non-finite numbers")
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, JsonValue] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ValueError("forensic journal detail keys must be non-empty strings")
            if raw_key in sanitized:
                raise ValueError(f"duplicate forensic journal detail key: {raw_key}")
            sanitized[raw_key] = _sanitize_json_value(
                raw_value,
                secret_context=_is_secret_key(raw_key),
            )
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize_json_value(item) for item in value]
    raise TypeError(
        "forensic journal details must contain only JSON-compatible values"
    )


def _sanitize_details(details: Mapping[str, object] | None) -> dict[str, JsonValue]:
    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise TypeError("details must be a mapping")
    sanitized = _sanitize_json_value(details)
    assert isinstance(sanitized, dict)
    return sanitized


def _parse_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ForensicJournalIntegrityError(
            "forensic journal occurred_at must be canonical UTC"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ForensicJournalIntegrityError(
            "forensic journal occurred_at is invalid"
        ) from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ForensicJournalIntegrityError(
            "forensic journal occurred_at must be UTC"
        )
    canonical = _canonical_timestamp(parsed)
    if canonical != value:
        raise ForensicJournalIntegrityError(
            "forensic journal occurred_at is not canonical"
        )
    return value


def _assert_safe_regular_or_absent(path: Path, *, label: str) -> None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ForensicJournalIntegrityError(
            f"{label} must be a regular non-aliased file"
        )


def _decode_json_object(raw: str, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except ForensicJournalIntegrityError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ForensicJournalIntegrityError(f"{label} is not valid JSON") from exc
    if type(payload) is not dict:
        raise ForensicJournalIntegrityError(f"{label} must be a JSON object")
    return payload


class ForensicSessionJournal:
    """Append-only, secret-safe, hash-chained process/session activity evidence.

    This journal is an observability/evidence boundary only. It does not replace
    economic, decision, provider, settlement, run-registry, or execution truth.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self.checkpoint_path = self.path.with_name(f"{self.path.name}.head.json")
        self._clock = clock
        if self.path == self.checkpoint_path:
            raise ValueError("journal and checkpoint paths must differ")
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with durable_path_lock(self.path):
            _assert_safe_regular_or_absent(self.path, label="forensic journal")
            _assert_safe_regular_or_absent(
                self.checkpoint_path, label="forensic journal checkpoint"
            )
            ensure_durable_file(self.path)
            if not self.checkpoint_path.exists():
                if self.path.stat().st_size != 0:
                    raise ForensicJournalIntegrityError(
                        "non-empty forensic journal is missing its checkpoint"
                    )
                atomic_write_json(
                    self.checkpoint_path,
                    self._checkpoint_payload(0, _GENESIS_SHA256),
                )
            self._load_state(recover_checkpoint=True)

    @staticmethod
    def _checkpoint_payload(record_count: int, last_record_sha256: str) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "record_count": record_count,
            "last_record_sha256": last_record_sha256,
        }

    def _read_checkpoint(self) -> tuple[int, str]:
        _assert_safe_regular_or_absent(
            self.checkpoint_path, label="forensic journal checkpoint"
        )
        try:
            raw = self.checkpoint_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ForensicJournalIntegrityError(
                "forensic journal checkpoint is unreadable"
            ) from exc
        payload = _decode_json_object(raw, label="forensic journal checkpoint")
        if set(payload) != _CHECKPOINT_KEYS:
            raise ForensicJournalIntegrityError(
                "forensic journal checkpoint schema is invalid"
            )
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ForensicJournalIntegrityError(
                "unsupported forensic journal checkpoint schema"
            )
        record_count = payload.get("record_count")
        if type(record_count) is not int or record_count < 0:
            raise ForensicJournalIntegrityError(
                "forensic journal checkpoint record_count is invalid"
            )
        last_sha = payload.get("last_record_sha256")
        if not _is_sha256(last_sha):
            raise ForensicJournalIntegrityError(
                "forensic journal checkpoint hash is invalid"
            )
        if record_count == 0 and last_sha != _GENESIS_SHA256:
            raise ForensicJournalIntegrityError(
                "empty forensic journal checkpoint must use genesis hash"
            )
        if record_count > 0 and last_sha == _GENESIS_SHA256:
            raise ForensicJournalIntegrityError(
                "non-empty forensic journal checkpoint cannot use genesis hash"
            )
        return record_count, last_sha

    def _read_records(self) -> list[ForensicJournalRecord]:
        _assert_safe_regular_or_absent(self.path, label="forensic journal")
        try:
            raw_bytes = self.path.read_bytes()
        except OSError as exc:
            raise ForensicJournalIntegrityError("forensic journal is unreadable") from exc
        if not raw_bytes:
            return []
        if not raw_bytes.endswith(b"\n"):
            raise ForensicJournalIntegrityError(
                "forensic journal has a truncated trailing record"
            )
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ForensicJournalIntegrityError(
                "forensic journal must be UTF-8"
            ) from exc

        records: list[ForensicJournalRecord] = []
        expected_previous = _GENESIS_SHA256
        for expected_sequence, line in enumerate(raw_text.splitlines(), start=1):
            if not line:
                raise ForensicJournalIntegrityError(
                    "forensic journal contains an empty record"
                )
            payload = _decode_json_object(
                line, label=f"forensic journal record {expected_sequence}"
            )
            record = self._validate_record(
                payload,
                expected_sequence=expected_sequence,
                expected_previous=expected_previous,
            )
            records.append(record)
            expected_previous = record.record_sha256
        return records

    def _validate_record(
        self,
        payload: dict[str, object],
        *,
        expected_sequence: int,
        expected_previous: str,
    ) -> ForensicJournalRecord:
        if set(payload) != _RECORD_KEYS:
            raise ForensicJournalIntegrityError("forensic journal record schema is invalid")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ForensicJournalIntegrityError(
                "unsupported forensic journal record schema"
            )
        if payload.get("sequence") != expected_sequence:
            raise ForensicJournalIntegrityError(
                "forensic journal sequence is not contiguous"
            )

        occurred_at = _parse_timestamp(payload.get("occurred_at"))
        source = payload.get("source")
        event_name = payload.get("event_name")
        try:
            source = _require_nonempty_text("source", source)
            event_name = _require_nonempty_text("event_name", event_name)
        except ValueError as exc:
            raise ForensicJournalIntegrityError(str(exc)) from exc

        try:
            kind = ForensicEventKind(payload.get("kind"))
        except (TypeError, ValueError) as exc:
            raise ForensicJournalIntegrityError(
                "forensic journal kind is invalid"
            ) from exc

        raw_state = payload.get("heartbeat_state")
        if kind is ForensicEventKind.HEARTBEAT:
            try:
                heartbeat_state = HeartbeatState(raw_state)
            except (TypeError, ValueError) as exc:
                raise ForensicJournalIntegrityError(
                    "heartbeat record requires a valid heartbeat state"
                ) from exc
        else:
            if raw_state is not None:
                raise ForensicJournalIntegrityError(
                    "non-heartbeat record cannot carry heartbeat state"
                )
            heartbeat_state = None

        message = payload.get("message")
        if message is not None:
            if not isinstance(message, str) or len(message) > 4096 or "\x00" in message:
                raise ForensicJournalIntegrityError(
                    "forensic journal message is invalid"
                )
            if _redact_text(message) != message:
                raise ForensicJournalIntegrityError(
                    "forensic journal message contains unredacted secret material"
                )

        details = payload.get("details")
        if type(details) is not dict:
            raise ForensicJournalIntegrityError(
                "forensic journal details must be an object"
            )
        try:
            sanitized_details = _sanitize_details(details)
        except (TypeError, ValueError) as exc:
            raise ForensicJournalIntegrityError(
                "forensic journal details are invalid"
            ) from exc
        if sanitized_details != details:
            raise ForensicJournalIntegrityError(
                "forensic journal details contain unredacted secret material"
            )

        previous = payload.get("previous_sha256")
        if previous != expected_previous:
            raise ForensicJournalIntegrityError(
                "forensic journal hash chain is discontinuous"
            )
        record_sha = payload.get("record_sha256")
        if not _is_sha256(record_sha):
            raise ForensicJournalIntegrityError(
                "forensic journal record hash is invalid"
            )
        digest_payload = dict(payload)
        del digest_payload["record_sha256"]
        calculated = _record_digest(digest_payload)
        if calculated != record_sha:
            raise ForensicJournalIntegrityError(
                "forensic journal record digest mismatch"
            )

        return ForensicJournalRecord(
            sequence=expected_sequence,
            occurred_at=occurred_at,
            kind=kind,
            source=source,
            event_name=event_name,
            heartbeat_state=heartbeat_state,
            message=message,
            details=details,
            previous_sha256=previous,
            record_sha256=record_sha,
        )

    def _load_state(
        self, *, recover_checkpoint: bool
    ) -> tuple[list[ForensicJournalRecord], ForensicJournalIntegrity]:
        records = self._read_records()
        checkpoint_count, checkpoint_sha = self._read_checkpoint()
        record_count = len(records)
        last_sha = records[-1].record_sha256 if records else _GENESIS_SHA256

        if checkpoint_count > record_count:
            raise ForensicJournalIntegrityError(
                "forensic journal tail was truncated behind its durable checkpoint"
            )
        if checkpoint_count == record_count:
            if checkpoint_sha != last_sha:
                raise ForensicJournalIntegrityError(
                    "forensic journal checkpoint does not match journal head"
                )
        else:
            expected_checkpoint_sha = (
                _GENESIS_SHA256
                if checkpoint_count == 0
                else records[checkpoint_count - 1].record_sha256
            )
            if checkpoint_sha != expected_checkpoint_sha:
                raise ForensicJournalIntegrityError(
                    "forensic journal checkpoint is not a valid journal prefix"
                )
            if not recover_checkpoint:
                raise ForensicJournalIntegrityError(
                    "forensic journal contains durable records beyond its checkpoint"
                )
            atomic_write_json(
                self.checkpoint_path,
                self._checkpoint_payload(record_count, last_sha),
            )

        return records, ForensicJournalIntegrity(
            record_count=record_count,
            last_record_sha256=last_sha,
        )

    def verify(self) -> ForensicJournalIntegrity:
        with durable_path_lock(self.path):
            _, integrity = self._load_state(recover_checkpoint=True)
            return integrity

    def read_records(self) -> tuple[ForensicJournalRecord, ...]:
        with durable_path_lock(self.path):
            records, _ = self._load_state(recover_checkpoint=True)
            return tuple(records)

    def _append(
        self,
        *,
        kind: ForensicEventKind,
        source: str,
        event_name: str,
        heartbeat_state: HeartbeatState | None,
        message: str | None,
        details: Mapping[str, object] | None,
    ) -> ForensicJournalRecord:
        source = _require_nonempty_text("source", source)
        event_name = _require_nonempty_text("event_name", event_name)
        if message is not None:
            if not isinstance(message, str):
                raise TypeError("message must be a string or None")
            if len(message) > 4096:
                raise ValueError("message is too long")
            if "\x00" in message:
                raise ValueError("message must not contain NUL")
            message = _redact_text(message)
        sanitized_details = _sanitize_details(details)
        occurred_at = _canonical_timestamp(self._clock())

        if kind is ForensicEventKind.HEARTBEAT:
            if not isinstance(heartbeat_state, HeartbeatState):
                raise TypeError("heartbeat_state must be HeartbeatState")
        elif heartbeat_state is not None:
            raise ValueError("non-heartbeat event cannot carry heartbeat_state")

        with durable_path_lock(self.path):
            records, _ = self._load_state(recover_checkpoint=True)
            sequence = len(records) + 1
            previous_sha256 = (
                records[-1].record_sha256 if records else _GENESIS_SHA256
            )
            payload: dict[str, object] = {
                "schema_version": _SCHEMA_VERSION,
                "sequence": sequence,
                "occurred_at": occurred_at,
                "kind": kind.value,
                "source": source,
                "heartbeat_state": (
                    heartbeat_state.value if heartbeat_state is not None else None
                ),
                "event_name": event_name,
                "message": message,
                "details": sanitized_details,
                "previous_sha256": previous_sha256,
            }
            record_sha256 = _record_digest(payload)
            payload["record_sha256"] = record_sha256
            line = _canonical_json_bytes(payload) + b"\n"

            _assert_safe_regular_or_absent(self.path, label="forensic journal")
            try:
                with self.path.open("ab") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise ForensicJournalError(
                    "failed to durably append forensic journal record"
                ) from exc

            atomic_write_json(
                self.checkpoint_path,
                self._checkpoint_payload(sequence, record_sha256),
            )
            return self._validate_record(
                payload,
                expected_sequence=sequence,
                expected_previous=previous_sha256,
            )

    def record_startup(
        self,
        source: str,
        *,
        message: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> ForensicJournalRecord:
        return self._append(
            kind=ForensicEventKind.STARTUP,
            source=source,
            event_name="startup",
            heartbeat_state=None,
            message=message,
            details=details,
        )

    def record_shutdown(
        self,
        source: str,
        *,
        message: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> ForensicJournalRecord:
        return self._append(
            kind=ForensicEventKind.SHUTDOWN,
            source=source,
            event_name="shutdown",
            heartbeat_state=None,
            message=message,
            details=details,
        )

    def record_crash(
        self,
        source: str,
        *,
        message: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> ForensicJournalRecord:
        return self._append(
            kind=ForensicEventKind.CRASH,
            source=source,
            event_name="crash",
            heartbeat_state=None,
            message=message,
            details=details,
        )

    def record_material_event(
        self,
        source: str,
        event_name: str,
        *,
        message: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> ForensicJournalRecord:
        return self._append(
            kind=ForensicEventKind.MATERIAL_EVENT,
            source=source,
            event_name=event_name,
            heartbeat_state=None,
            message=message,
            details=details,
        )

    def record_heartbeat(
        self,
        source: str,
        state: HeartbeatState,
        *,
        message: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> ForensicJournalRecord:
        if not isinstance(state, HeartbeatState):
            raise TypeError("state must be HeartbeatState")
        return self._append(
            kind=ForensicEventKind.HEARTBEAT,
            source=source,
            event_name="heartbeat",
            heartbeat_state=state,
            message=message,
            details=details,
        )

    def export(self, destination: str | Path) -> Path:
        destination = Path(destination)
        if destination in {self.path, self.checkpoint_path}:
            raise ValueError("export destination must differ from journal/checkpoint")
        with durable_path_lock(self.path):
            records, integrity = self._load_state(recover_checkpoint=True)
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "record_count": integrity.record_count,
                "last_record_sha256": integrity.last_record_sha256,
                "records": [
                    {
                        "sequence": record.sequence,
                        "occurred_at": record.occurred_at,
                        "kind": record.kind.value,
                        "source": record.source,
                        "heartbeat_state": (
                            record.heartbeat_state.value
                            if record.heartbeat_state is not None
                            else None
                        ),
                        "event_name": record.event_name,
                        "message": record.message,
                        "details": record.details,
                        "previous_sha256": record.previous_sha256,
                        "record_sha256": record.record_sha256,
                    }
                    for record in records
                ],
            }
            atomic_write_json(destination, payload)
        return destination
