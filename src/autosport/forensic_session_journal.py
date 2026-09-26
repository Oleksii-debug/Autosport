from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA_VERSION = 1
GENESIS_SHA256 = "0" * 64
REDACTED = "[REDACTED]"
_LOCK_MAGIC = b"AUTOSPORT_FORENSIC_SESSION_LOCK_V1\n"
_LOCK_NEW_MAGIC = b"AUTOSPORT_FORENSIC_SESSION_LOCK_NEW_V1\n"
_CHECKPOINT_KEYS = frozenset(
    {"schema_version", "record_count", "last_record_sha256"}
)
_CHECKPOINT_MAX_BYTES = 4096

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


class JournalLockedError(JournalError):
    """Raised when another process already owns the journal writer lease."""


def _acquire_process_lock(fd: int) -> None:
    """Acquire a non-blocking OS lock for the first byte of the sidecar file."""
    if os.name == "nt":
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise JournalLockedError("forensic session journal already has an active writer") from exc
        return

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise JournalLockedError("forensic session journal already has an active writer") from exc


def _release_process_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


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
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise JournalIntegrityError("journal timestamp is not canonical UTC")
    return value


def _validate_event_type(value: Any) -> str:
    if not isinstance(value, str) or not _EVENT_RE.fullmatch(value):
        raise ValueError(f"invalid journal event_type: {value!r}")
    return value


def _is_sensitive_key(key: str) -> bool:
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key.strip())
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")
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


def _checkpoint_path(journal_path: Path) -> Path:
    return journal_path.with_name(journal_path.name + ".head.json")


def _checkpoint_payload(record_count: int, last_record_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_count": record_count,
        "last_record_sha256": last_record_sha256,
    }


def _read_checkpoint(path: Path) -> tuple[int, str] | None:
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(lst.st_mode) or lst.st_nlink != 1:
        raise JournalIntegrityError(
            "forensic session journal checkpoint must be a single-link regular file"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise JournalIntegrityError(
            "forensic session journal checkpoint is not safely readable"
        ) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise JournalIntegrityError(
                "forensic session journal checkpoint identity is unsafe"
            )
        chunks: list[bytes] = []
        remaining = _CHECKPOINT_MAX_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)

    if len(raw) > _CHECKPOINT_MAX_BYTES:
        raise JournalIntegrityError("forensic session journal checkpoint is oversized")
    if not raw.endswith(b"\n"):
        raise JournalIntegrityError(
            "forensic session journal checkpoint is not canonically terminated"
        )
    try:
        text = raw[:-1].decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise JournalIntegrityError(
            "forensic session journal checkpoint is not valid UTF-8"
        ) from exc
    except json.JSONDecodeError as exc:
        raise JournalIntegrityError(
            "forensic session journal checkpoint is not valid JSON"
        ) from exc

    if type(payload) is not dict or set(payload) != _CHECKPOINT_KEYS:
        raise JournalIntegrityError(
            "forensic session journal checkpoint schema drift detected"
        )
    if raw != _canonical_json(payload) + b"\n":
        raise JournalIntegrityError(
            "forensic session journal checkpoint is not canonical JSON"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise JournalIntegrityError(
            "unsupported forensic session journal checkpoint schema version"
        )
    count = payload.get("record_count")
    digest = payload.get("last_record_sha256")
    if type(count) is not int or count < 0:
        raise JournalIntegrityError(
            "forensic session journal checkpoint record_count is invalid"
        )
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise JournalIntegrityError(
            "forensic session journal checkpoint digest is invalid"
        )
    if (count == 0) != (digest == GENESIS_SHA256):
        raise JournalIntegrityError(
            "forensic session journal checkpoint genesis state is inconsistent"
        )
    return count, digest


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_checkpoint(path: Path, record_count: int, digest: str) -> None:
    payload = _checkpoint_payload(record_count, digest)
    serialized = _canonical_json(payload) + b"\n"
    existing = _read_checkpoint(path)
    if existing is not None and existing == (record_count, digest):
        return

    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd: int | None = None
    try:
        fd = os.open(temp_path, flags, 0o600)
        view = memoryview(serialized)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("checkpoint write made no progress")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temp_path, path)
        _fsync_parent_directory(path)
        if _read_checkpoint(path) != (record_count, digest):
            raise OSError("checkpoint publication did not preserve intended head")
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _reconcile_checkpoint(
    journal_path: Path,
    records: tuple[JournalRecord, ...] | list[JournalRecord],
    *,
    recover: bool,
) -> None:
    path = _checkpoint_path(journal_path)
    checkpoint = _read_checkpoint(path)
    count = len(records)
    digest = records[-1].sha256 if records else GENESIS_SHA256

    if checkpoint is None:
        if count:
            raise JournalIntegrityError(
                "non-empty forensic session journal is missing its durable checkpoint"
            )
        if recover:
            _write_checkpoint(path, 0, GENESIS_SHA256)
        return

    checkpoint_count, checkpoint_digest = checkpoint
    if checkpoint_count > count:
        raise JournalIntegrityError(
            "forensic session journal tail was truncated behind its durable checkpoint"
        )
    if checkpoint_count == count:
        if checkpoint_digest != digest:
            raise JournalIntegrityError(
                "forensic session journal checkpoint does not match journal head"
            )
        return

    prefix_digest = (
        GENESIS_SHA256
        if checkpoint_count == 0
        else records[checkpoint_count - 1].sha256
    )
    if checkpoint_digest != prefix_digest:
        raise JournalIntegrityError(
            "forensic session journal checkpoint is not a valid journal prefix"
        )
    if not recover:
        raise JournalIntegrityError(
            "forensic session journal contains durable records beyond its checkpoint"
        )
    _write_checkpoint(path, count, digest)


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


def _parse_verified_record_bytes(raw: bytes) -> tuple[JournalRecord, ...]:
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


def _read_verified_records_only(path: str | os.PathLike[str]) -> tuple[JournalRecord, ...]:
    journal_path = Path(path)
    if not journal_path.exists():
        return ()
    return _parse_verified_record_bytes(journal_path.read_bytes())


def _assert_bound_journal_path(
    path: Path,
    identity: tuple[int, int],
    size: int,
) -> None:
    try:
        current = os.lstat(path)
    except FileNotFoundError as exc:
        raise JournalIntegrityError(
            "journal path disappeared during verification"
        ) from exc
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise JournalIntegrityError(
            "journal path identity is not a single-link regular file"
        )
    if (current.st_dev, current.st_ino) != identity:
        raise JournalIntegrityError("journal path identity changed during verification")
    if current.st_size != size:
        raise JournalIntegrityError("journal file size changed during verification")


def _read_verified_records_bound(
    path: Path,
) -> tuple[tuple[JournalRecord, ...], tuple[int, int] | None, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return (), None, 0
    except OSError as exc:
        raise JournalIntegrityError("journal path is not safely openable") from exc

    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise JournalIntegrityError(
                "journal verification requires a single-link regular file"
            )
        identity = (before.st_dev, before.st_ino)

        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)

        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != identity or after.st_nlink != 1:
            raise JournalIntegrityError("journal inode changed during verification")
        if before.st_size != after.st_size or len(raw) != after.st_size:
            raise JournalIntegrityError("journal file size changed during verification")
        if before.st_mtime_ns != after.st_mtime_ns:
            raise JournalIntegrityError("journal content changed during verification")

        records = _parse_verified_record_bytes(raw)
        _assert_bound_journal_path(path, identity, after.st_size)
        return records, identity, after.st_size
    finally:
        os.close(fd)


def read_verified_records(path: str | os.PathLike[str]) -> tuple[JournalRecord, ...]:
    journal_path = Path(path)
    records = _read_verified_records_only(journal_path)
    _reconcile_checkpoint(journal_path, records, recover=False)
    return records


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
        requested_path = Path(path)
        requested_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = requested_path.resolve(strict=False)
        if self._path.exists():
            st = self._path.stat()
            if not stat.S_ISREG(st.st_mode):
                raise JournalIntegrityError("journal path must resolve to a regular file")
            if st.st_nlink != 1:
                raise JournalIntegrityError("hard-linked journal paths are not supported")
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._uncertain = False
        self._writer_lock_fd: int | None = None
        self._writer_lock_identity: tuple[int, int] | None = None
        self._writer_lock_created = False
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        self._checkpoint_path = _checkpoint_path(self._path)
        self._expected_file_identity: tuple[int, int] | None = None
        self._expected_file_size = 0
        self._acquire_writer_lock()
        try:
            records, verified_identity, verified_size = _read_verified_records_bound(
                self._path
            )
            self._records = list(records)
            if self._writer_lock_created and self._records:
                self._discard_fresh_writer_lock_path()
                raise JournalIntegrityError(
                    "journal exists but durable writer lock sidecar was missing; "
                    "ownership continuity is ambiguous"
                )
            if self._writer_lock_created:
                self._bind_fresh_writer_lock()

            # Adopt only the inode whose bytes were actually verified. A later
            # pathname stat must never be allowed to bless a replacement file.
            self._expected_file_identity = verified_identity
            self._expected_file_size = verified_size
            _reconcile_checkpoint(self._path, self._records, recover=True)
            if verified_identity is not None:
                _assert_bound_journal_path(
                    self._path, verified_identity, verified_size
                )
            self._seq = len(self._records)
            self._last_sha256 = self._records[-1].sha256 if self._records else GENESIS_SHA256

            if session_id is None:
                self._session_id = str(uuid.uuid4())
            else:
                try:
                    parsed = str(uuid.UUID(session_id))
                except (ValueError, AttributeError, TypeError) as exc:
                    raise ValueError("session_id must be canonical UUID text") from exc
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
        except BaseException:
            self._release_writer_lock()
            raise

    @property
    def path(self) -> Path:
        return self._path

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def checkpoint_path(self) -> Path:
        return self._checkpoint_path

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @staticmethod
    def _write_lock_marker(fd: int, marker: bytes) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        view = memoryview(marker)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("writer lock sidecar marker write made no progress")
            view = view[written:]
        os.fsync(fd)

    @staticmethod
    def _read_lock_marker(fd: int) -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, max(len(_LOCK_MAGIC), len(_LOCK_NEW_MAGIC)) + 1)

    def _try_publish_locked_sidecar(
        self, base_flags: int
    ) -> tuple[int, tuple[int, int]] | None:
        temp_path = self._lock_path.with_name(
            f".{self._lock_path.name}.{uuid.uuid4().hex}.claim"
        )
        try:
            fd = os.open(temp_path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise JournalIntegrityError("writer lock sidecar staging file is not creatable") from exc

        locked = False
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise JournalIntegrityError("writer lock sidecar staging file is not regular")
            self._write_lock_marker(fd, _LOCK_NEW_MAGIC)
            _acquire_process_lock(fd)
            locked = True
            try:
                os.link(temp_path, self._lock_path)
            except FileExistsError:
                _release_process_lock(fd)
                locked = False
                os.close(fd)
                fd = -1
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                return None
            except OSError as exc:
                raise JournalIntegrityError("writer lock sidecar cannot be published atomically") from exc

            os.unlink(temp_path)
            current = os.lstat(self._lock_path)
            identity = (opened.st_dev, opened.st_ino)
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
                raise JournalIntegrityError("writer lock sidecar identity is ambiguous after publish")
            return fd, identity
        except BaseException:
            if fd >= 0:
                if locked:
                    try:
                        _release_process_lock(fd)
                    except OSError:
                        pass
                os.close(fd)
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

    def _acquire_writer_lock(self) -> None:
        base_flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)

        for _attempt in range(8):
            try:
                staged = self._try_publish_locked_sidecar(base_flags)
                if staged is not None:
                    staged_fd, staged_identity = staged
                    self._writer_lock_fd = staged_fd
                    self._writer_lock_identity = staged_identity
                    self._writer_lock_created = True
                    return
            except FileNotFoundError:
                continue

            try:
                fd = os.open(self._lock_path, base_flags, 0o600)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise JournalIntegrityError("writer lock sidecar is not safely openable") from exc

            locked = False
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise JournalIntegrityError("writer lock sidecar must be a regular file")
                _acquire_process_lock(fd)
                locked = True
                marker = self._read_lock_marker(fd)
                if marker not in {_LOCK_MAGIC, _LOCK_NEW_MAGIC}:
                    raise JournalIntegrityError("writer lock sidecar marker is invalid")

                current = os.lstat(self._lock_path)
                identity = (opened.st_dev, opened.st_ino)
                if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
                    raise JournalIntegrityError("writer lock sidecar identity is ambiguous")
            except BaseException:
                if locked:
                    try:
                        _release_process_lock(fd)
                    except OSError:
                        pass
                os.close(fd)
                raise

            self._writer_lock_fd = fd
            self._writer_lock_identity = identity
            self._writer_lock_created = marker == _LOCK_NEW_MAGIC
            return

        raise JournalIntegrityError("writer lock sidecar path was unstable during acquisition")

    def _discard_fresh_writer_lock_path(self) -> None:
        """Remove a newly published unbound sidecar while its inode is still locked.

        If the platform refuses deletion, the NEW marker remains fail-closed: later
        writers will continue to reject it for a non-empty journal.
        """
        if not self._writer_lock_created or self._writer_lock_identity is None:
            return
        try:
            current = os.lstat(self._lock_path)
            if (current.st_dev, current.st_ino) == self._writer_lock_identity:
                os.unlink(self._lock_path)
        except OSError:
            pass

    def _bind_fresh_writer_lock(self) -> None:
        if not self._writer_lock_created:
            return
        fd = self._writer_lock_fd
        if fd is None:
            raise JournalLockedError("forensic session journal writer lock is not held")
        try:
            self._assert_writer_lock_continuity()
            self._write_lock_marker(fd, _LOCK_MAGIC)
        except OSError as exc:
            self._uncertain = True
            self._release_writer_lock()
            raise JournalUncertainError(
                "writer lock sidecar durability is uncertain; reopen and reverify before continuing"
            ) from exc
        self._writer_lock_created = False

    def _assert_writer_lock_continuity(self) -> None:
        fd = self._writer_lock_fd
        identity = self._writer_lock_identity
        if fd is None or identity is None:
            raise JournalLockedError("forensic session journal writer lock is not held")
        try:
            current = os.lstat(self._lock_path)
        except FileNotFoundError as exc:
            raise JournalIntegrityError("writer lock sidecar disappeared while writer was active") from exc
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            raise JournalIntegrityError("writer lock sidecar identity changed while writer was active")

    def _release_writer_lock(self) -> None:
        fd = self._writer_lock_fd
        if fd is None:
            return
        self._writer_lock_fd = None
        self._writer_lock_identity = None
        try:
            _release_process_lock(fd)
        finally:
            os.close(fd)

    def _append_locked(self, event_type: str, payload: Mapping[str, Any] | None) -> JournalRecord:
        if self._uncertain:
            raise JournalUncertainError(
                "journal append durability is uncertain; reopen and reverify before continuing"
            )
        if self._closed:
            raise JournalClosedError("forensic session journal is closed")
        if self._writer_lock_fd is None:
            raise JournalLockedError("forensic session journal writer lock is not held")
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

        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if self._expected_file_identity is None:
            flags |= os.O_CREAT | os.O_EXCL

        fd: int | None = None
        pre_size = self._expected_file_size
        try:
            self._assert_writer_lock_continuity()
            fd = os.open(self._path, flags, 0o600)
            pre = os.fstat(fd)
            if not stat.S_ISREG(pre.st_mode):
                raise JournalIntegrityError("journal path no longer resolves to a regular file")
            if pre.st_nlink != 1:
                raise JournalIntegrityError("journal file link count changed while writer was active")
            identity = (pre.st_dev, pre.st_ino)
            if self._expected_file_identity is not None and identity != self._expected_file_identity:
                raise JournalIntegrityError("journal file identity changed while writer was active")
            if pre.st_size != pre_size:
                raise JournalIntegrityError("journal file size changed while writer was active")

            view = memoryview(serialized)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("journal append made no progress")
                view = view[written:]
            os.fsync(fd)
            post = os.fstat(fd)
            if post.st_size != pre_size + len(serialized):
                raise OSError("journal append size is inconsistent after fsync")
            self._expected_file_identity = identity
            self._expected_file_size = post.st_size
            os.close(fd)
            fd = None
            self._assert_writer_lock_continuity()
            _write_checkpoint(self._checkpoint_path, seq, digest)
            self._assert_writer_lock_continuity()
        except JournalIntegrityError:
            self._uncertain = True
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._release_writer_lock()
            raise
        except OSError as exc:
            self._uncertain = True
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._release_writer_lock()
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
            self._release_writer_lock()
            return record

    def __enter__(self) -> "ForensicSessionJournal":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close({"exit": "error" if exc_type is not None else "clean"})
        return False
