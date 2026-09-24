from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .integrity import durable_path_lock


_SCHEMA_VERSION = 1
_ZERO_SHA256 = "0" * 64
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
_MAX_ID_BYTES = 512


class FeedContinuityError(RuntimeError):
    """Raised when live feed continuity cannot be proven safely."""


class SequenceMode(str, Enum):
    CONTIGUOUS_INT = "CONTIGUOUS_INT"


class ContinuityStatus(str, Enum):
    HEALTHY = "HEALTHY"
    RESYNC_REQUIRED = "RESYNC_REQUIRED"


class ContinuityFault(str, Enum):
    EVENT_ID_CONFLICT = "EVENT_ID_CONFLICT"
    SEQUENCE_MODE_CHANGED = "SEQUENCE_MODE_CHANGED"
    SEQUENCE_REGRESSION = "SEQUENCE_REGRESSION"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    RECEIVE_MONOTONIC_REGRESSION = "RECEIVE_MONOTONIC_REGRESSION"
    RECEIVE_GAP = "RECEIVE_GAP"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FeedContinuityError(f"duplicate JSON key in continuity journal: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise FeedContinuityError(f"non-finite JSON value in continuity journal: {value}")


def _validate_id(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty trimmed string")
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) > _MAX_ID_BYTES:
        raise ValueError(f"{name} must be at most {_MAX_ID_BYTES} UTF-8 bytes")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _validate_int64(name: str, value: object) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise TypeError(f"{name} must be an exact non-boolean int")
    if value < _INT64_MIN or value > _INT64_MAX:
        raise ValueError(f"{name} must fit signed 64-bit integer range")
    return value


def _validate_nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise TypeError(f"{name} must be an exact non-boolean int")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True, slots=True)
class FeedContinuityObservation:
    source_id: str
    stream_id: str
    event_id: str
    sequence_mode: SequenceMode
    sequence: int
    received_monotonic_ns: int

    def __post_init__(self) -> None:
        _validate_id("source_id", self.source_id)
        _validate_id("stream_id", self.stream_id)
        _validate_id("event_id", self.event_id)
        if not isinstance(self.sequence_mode, SequenceMode):
            raise TypeError("sequence_mode must be a SequenceMode")
        _validate_int64("sequence", self.sequence)
        _validate_nonnegative_int("received_monotonic_ns", self.received_monotonic_ns)

    @property
    def stream_key(self) -> tuple[str, str]:
        return self.source_id, self.stream_id

    def payload(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "stream_id": self.stream_id,
            "event_id": self.event_id,
            "sequence_mode": self.sequence_mode.value,
            "sequence": self.sequence,
            "received_monotonic_ns": self.received_monotonic_ns,
        }

    @property
    def evidence_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.payload())).hexdigest()


@dataclass(frozen=True, slots=True)
class FeedContinuityState:
    source_id: str
    stream_id: str
    status: ContinuityStatus
    sequence_mode: SequenceMode | None
    last_sequence: int | None
    last_received_monotonic_ns: int | None
    accepted_event_count: int
    fault: ContinuityFault | None
    fault_event_id: str | None
    journal_record_count: int
    journal_head_sha256: str

    @property
    def healthy(self) -> bool:
        return self.status is ContinuityStatus.HEALTHY


@dataclass(slots=True)
class _MutableStreamState:
    source_id: str
    stream_id: str
    status: ContinuityStatus = ContinuityStatus.HEALTHY
    sequence_mode: SequenceMode | None = None
    last_sequence: int | None = None
    last_received_monotonic_ns: int | None = None
    accepted_event_count: int = 0
    fault: ContinuityFault | None = None
    fault_event_id: str | None = None

    def public(self, *, record_count: int, head_sha256: str) -> FeedContinuityState:
        return FeedContinuityState(
            source_id=self.source_id,
            stream_id=self.stream_id,
            status=self.status,
            sequence_mode=self.sequence_mode,
            last_sequence=self.last_sequence,
            last_received_monotonic_ns=self.last_received_monotonic_ns,
            accepted_event_count=self.accepted_event_count,
            fault=self.fault,
            fault_event_id=self.fault_event_id,
            journal_record_count=record_count,
            journal_head_sha256=head_sha256,
        )


class FeedContinuityJournal:
    """Durable fail-closed continuity witness for contiguous integer feed streams.

    This authority deliberately excludes provider wall-clock freshness, quote age,
    opaque provider cursor interpretation, market actionability, and resync authority.
    Once continuity is lost for a stream identity, that stream remains quarantined.
    A caller must establish a new authoritative stream identity outside this module.
    """

    def __init__(self, path: Path | str, *, max_receive_gap_ns: int | None = None) -> None:
        self._path = Path(path)
        if max_receive_gap_ns is not None:
            _validate_nonnegative_int("max_receive_gap_ns", max_receive_gap_ns)
        self._max_receive_gap_ns = max_receive_gap_ns
        self._lock = threading.RLock()
        self._streams: dict[tuple[str, str], _MutableStreamState] = {}
        self._events: dict[tuple[str, str, str], str] = {}
        self._record_count = 0
        self._head_sha256 = _ZERO_SHA256
        self._known_size = 0
        with durable_path_lock(self._path):
            self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record_count(self) -> int:
        return self._record_count

    @property
    def head_sha256(self) -> str:
        return self._head_sha256

    def state(self, source_id: str, stream_id: str) -> FeedContinuityState:
        source = _validate_id("source_id", source_id)
        stream = _validate_id("stream_id", stream_id)
        with self._lock:
            mutable = self._streams.get((source, stream))
            if mutable is None:
                mutable = _MutableStreamState(source_id=source, stream_id=stream)
            return mutable.public(
                record_count=self._record_count,
                head_sha256=self._head_sha256,
            )

    def observe(self, observation: FeedContinuityObservation) -> FeedContinuityState:
        if not isinstance(observation, FeedContinuityObservation):
            raise TypeError("observation must be a FeedContinuityObservation")
        with self._lock, durable_path_lock(self._path):
            self._assert_disk_generation_unchanged()
            key = observation.stream_key
            event_key = (observation.source_id, observation.stream_id, observation.event_id)
            evidence_sha = observation.evidence_sha256
            existing_event_sha = self._events.get(event_key)
            if existing_event_sha is not None:
                if existing_event_sha == evidence_sha:
                    return self.state(*key)
                self._record_fault(observation, ContinuityFault.EVENT_ID_CONFLICT)
                raise FeedContinuityError("event_id replay conflicts with durable observation")

            current = self._streams.get(key)
            if current is None:
                current = _MutableStreamState(
                    source_id=observation.source_id,
                    stream_id=observation.stream_id,
                )
                self._streams[key] = current
            if current.status is ContinuityStatus.RESYNC_REQUIRED:
                raise FeedContinuityError("stream continuity is quarantined; authoritative resync required")

            fault = self._continuity_fault(current, observation)
            if fault is not None:
                self._record_fault(observation, fault)
                raise FeedContinuityError(f"feed continuity lost: {fault.value}")

            payload = {
                "kind": "observation",
                "observation": observation.payload(),
                "evidence_sha256": evidence_sha,
            }
            self._append_record(payload)
            self._apply_observation(observation, evidence_sha)
            return current.public(
                record_count=self._record_count,
                head_sha256=self._head_sha256,
            )

    def _continuity_fault(
        self,
        current: _MutableStreamState,
        observation: FeedContinuityObservation,
    ) -> ContinuityFault | None:
        if current.accepted_event_count == 0:
            return None
        if current.sequence_mode is not observation.sequence_mode:
            return ContinuityFault.SEQUENCE_MODE_CHANGED
        assert current.last_sequence is not None
        assert current.last_received_monotonic_ns is not None
        if observation.received_monotonic_ns < current.last_received_monotonic_ns:
            return ContinuityFault.RECEIVE_MONOTONIC_REGRESSION
        if (
            self._max_receive_gap_ns is not None
            and observation.received_monotonic_ns - current.last_received_monotonic_ns
            > self._max_receive_gap_ns
        ):
            return ContinuityFault.RECEIVE_GAP
        expected = current.last_sequence + 1
        if expected > _INT64_MAX:
            return ContinuityFault.SEQUENCE_GAP
        if observation.sequence < expected:
            return ContinuityFault.SEQUENCE_REGRESSION
        if observation.sequence > expected:
            return ContinuityFault.SEQUENCE_GAP
        return None

    def _record_fault(self, observation: FeedContinuityObservation, fault: ContinuityFault) -> None:
        key = observation.stream_key
        current = self._streams.get(key)
        if current is None:
            current = _MutableStreamState(
                source_id=observation.source_id,
                stream_id=observation.stream_id,
            )
            self._streams[key] = current
        payload = {
            "kind": "fault",
            "source_id": observation.source_id,
            "stream_id": observation.stream_id,
            "event_id": observation.event_id,
            "fault": fault.value,
            "attempted_evidence_sha256": observation.evidence_sha256,
        }
        self._append_record(payload)
        current.status = ContinuityStatus.RESYNC_REQUIRED
        current.fault = fault
        current.fault_event_id = observation.event_id

    def _apply_observation(self, observation: FeedContinuityObservation, evidence_sha: str) -> None:
        key = observation.stream_key
        current = self._streams[key]
        current.sequence_mode = observation.sequence_mode
        current.last_sequence = observation.sequence
        current.last_received_monotonic_ns = observation.received_monotonic_ns
        current.accepted_event_count += 1
        self._events[(observation.source_id, observation.stream_id, observation.event_id)] = evidence_sha

    def _validate_existing_journal_path(self) -> os.stat_result | None:
        try:
            path_stat = os.stat(self._path, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FeedContinuityError("cannot inspect continuity journal path") from exc
        if not stat.S_ISREG(path_stat.st_mode):
            raise FeedContinuityError("continuity journal path must be a regular non-symlink file")
        if path_stat.st_nlink != 1:
            raise FeedContinuityError("continuity journal path must not have hard-link aliases")
        return path_stat

    def _assert_disk_generation_unchanged(self) -> None:
        path_stat = self._validate_existing_journal_path()
        current_size = 0 if path_stat is None else path_stat.st_size
        if current_size != self._known_size:
            raise FeedContinuityError(
                "continuity journal changed by another writer; reopen before continuing"
            )

    def _append_record(self, payload: dict[str, object]) -> None:
        next_index = self._record_count + 1
        unsigned = {
            "schema": _SCHEMA_VERSION,
            "record_index": next_index,
            "prev_sha256": self._head_sha256,
            "payload": payload,
        }
        record_sha256 = hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()
        record = {**unsigned, "record_sha256": record_sha256}
        encoded = _canonical_json_bytes(record) + b"\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_existing_journal_path()
        with self._path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self._fsync_parent()
        self._record_count = next_index
        self._head_sha256 = record_sha256
        self._known_size += len(encoded)

    def _fsync_parent(self) -> None:
        try:
            fd = os.open(self._path.parent, os.O_RDONLY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _load(self) -> None:
        path_stat = self._validate_existing_journal_path()
        if path_stat is None:
            self._known_size = 0
            return
        raw = self._path.read_bytes()
        self._known_size = len(raw)
        if not raw:
            return
        if not raw.endswith(b"\n"):
            raise FeedContinuityError("continuity journal is truncated: missing final newline")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FeedContinuityError("continuity journal is not valid UTF-8") from exc

        for expected_index, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise FeedContinuityError("continuity journal contains an empty record")
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
            except FeedContinuityError:
                raise
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise FeedContinuityError("continuity journal contains invalid JSON") from exc
            self._replay_record(record, expected_index)

    def _replay_record(self, record: object, expected_index: int) -> None:
        if not isinstance(record, dict):
            raise FeedContinuityError("continuity journal record must be an object")
        required = {"schema", "record_index", "prev_sha256", "payload", "record_sha256"}
        if set(record) != required:
            raise FeedContinuityError("continuity journal record has unexpected fields")
        if record["schema"] != _SCHEMA_VERSION:
            raise FeedContinuityError("unsupported continuity journal schema")
        if record["record_index"] != expected_index:
            raise FeedContinuityError("continuity journal record index is not contiguous")
        if record["prev_sha256"] != self._head_sha256:
            raise FeedContinuityError("continuity journal hash chain is broken")
        record_sha = record["record_sha256"]
        if not isinstance(record_sha, str) or len(record_sha) != 64:
            raise FeedContinuityError("continuity journal record_sha256 is invalid")
        unsigned = {
            "schema": record["schema"],
            "record_index": record["record_index"],
            "prev_sha256": record["prev_sha256"],
            "payload": record["payload"],
        }
        expected_sha = hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()
        if record_sha != expected_sha:
            raise FeedContinuityError("continuity journal record digest mismatch")
        payload = record["payload"]
        if not isinstance(payload, dict):
            raise FeedContinuityError("continuity journal payload must be an object")
        kind = payload.get("kind")
        if kind == "observation":
            self._replay_observation_payload(payload)
        elif kind == "fault":
            self._replay_fault_payload(payload)
        else:
            raise FeedContinuityError("continuity journal record kind is invalid")
        self._record_count = expected_index
        self._head_sha256 = record_sha

    def _replay_observation_payload(self, payload: dict[str, object]) -> None:
        if set(payload) != {"kind", "observation", "evidence_sha256"}:
            raise FeedContinuityError("continuity observation payload has unexpected fields")
        raw_observation = payload["observation"]
        if not isinstance(raw_observation, dict):
            raise FeedContinuityError("continuity observation must be an object")
        if set(raw_observation) != {
            "source_id",
            "stream_id",
            "event_id",
            "sequence_mode",
            "sequence",
            "received_monotonic_ns",
        }:
            raise FeedContinuityError("continuity observation has unexpected fields")
        try:
            observation = FeedContinuityObservation(
                source_id=raw_observation["source_id"],
                stream_id=raw_observation["stream_id"],
                event_id=raw_observation["event_id"],
                sequence_mode=SequenceMode(raw_observation["sequence_mode"]),
                sequence=raw_observation["sequence"],
                received_monotonic_ns=raw_observation["received_monotonic_ns"],
            )
        except (TypeError, ValueError) as exc:
            raise FeedContinuityError("continuity observation fields are invalid") from exc
        evidence_sha = payload["evidence_sha256"]
        if evidence_sha != observation.evidence_sha256:
            raise FeedContinuityError("continuity observation evidence digest mismatch")
        event_key = (observation.source_id, observation.stream_id, observation.event_id)
        if event_key in self._events:
            raise FeedContinuityError("continuity journal repeats an accepted event_id")
        key = observation.stream_key
        current = self._streams.get(key)
        if current is None:
            current = _MutableStreamState(
                source_id=observation.source_id,
                stream_id=observation.stream_id,
            )
            self._streams[key] = current
        if current.status is ContinuityStatus.RESYNC_REQUIRED:
            raise FeedContinuityError("accepted observation appears after durable continuity fault")
        fault = self._continuity_fault(current, observation)
        if fault is not None:
            raise FeedContinuityError(
                f"durable observation violates continuity without fault record: {fault.value}"
            )
        self._apply_observation(observation, evidence_sha)

    def _replay_fault_payload(self, payload: dict[str, object]) -> None:
        if set(payload) != {
            "kind",
            "source_id",
            "stream_id",
            "event_id",
            "fault",
            "attempted_evidence_sha256",
        }:
            raise FeedContinuityError("continuity fault payload has unexpected fields")
        source_id = _validate_id("source_id", payload["source_id"])
        stream_id = _validate_id("stream_id", payload["stream_id"])
        event_id = _validate_id("event_id", payload["event_id"])
        try:
            fault = ContinuityFault(payload["fault"])
        except (TypeError, ValueError) as exc:
            raise FeedContinuityError("continuity fault reason is invalid") from exc
        attempted_sha = payload["attempted_evidence_sha256"]
        if not isinstance(attempted_sha, str) or len(attempted_sha) != 64:
            raise FeedContinuityError("continuity attempted evidence digest is invalid")
        key = (source_id, stream_id)
        current = self._streams.get(key)
        if current is None:
            current = _MutableStreamState(source_id=source_id, stream_id=stream_id)
            self._streams[key] = current
        if current.status is ContinuityStatus.RESYNC_REQUIRED:
            raise FeedContinuityError("continuity journal contains multiple faults after quarantine")
        current.status = ContinuityStatus.RESYNC_REQUIRED
        current.fault = fault
        current.fault_event_id = event_id
