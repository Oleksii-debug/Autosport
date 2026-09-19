from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping


class CausalCollectorError(ValueError):
    pass


class DeltaConflictError(CausalCollectorError):
    pass


class CursorRegressionError(CausalCollectorError):
    pass


class GapStateError(CausalCollectorError):
    pass


class AckConflictError(CausalCollectorError):
    pass


class CausalView(StrEnum):
    AS_KNOWN_AT_DECISION = "AS_KNOWN_AT_DECISION"
    RESTATED_RESEARCH = "RESTATED_RESEARCH"


class GapState(StrEnum):
    NONE = "NONE"
    DETECTED = "DETECTED"
    RECOVERED = "RECOVERED"
    CURSOR_RESET = "CURSOR_RESET"


def _instant(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be valid ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware ISO-8601")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def canonical_event_digest(event_or_payload: Any) -> str:
    payload = event_or_payload.to_dict() if hasattr(event_or_payload, "to_dict") else event_or_payload
    try:
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("canonical event payload is not JSON-safe") from exc
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class CollectorDelta:
    schema_version: int
    delta_id: str
    source_id: str
    lawful_terms_ref: str
    retention_ref: str
    stream_epoch: str
    source_cursor: str
    cursor_position: int
    event_dedupe_key: str
    event_id: str
    canonical_event_digest: str
    source_observed_at: str
    collector_received_at: str
    collector_committed_at: str
    desktop_available_at: str
    revision_of: str | None = None
    revision_number: int = 0
    quality_flags: tuple[str, ...] = ()
    gap_state: GapState = GapState.NONE
    gap_from_cursor: str | None = None
    gap_to_cursor: str | None = None

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported collector delta schema_version")
        for name in (
            "delta_id", "source_id", "lawful_terms_ref", "retention_ref", "stream_epoch",
            "source_cursor", "event_dedupe_key", "event_id"
        ):
            _text(getattr(self, name), name)
        if isinstance(self.cursor_position, bool) or not isinstance(self.cursor_position, int) or self.cursor_position < 0:
            raise ValueError("cursor_position must be a non-negative int")
        if isinstance(self.revision_number, bool) or not isinstance(self.revision_number, int) or self.revision_number < 0:
            raise ValueError("revision_number must be a non-negative int")
        if self.revision_number and not self.revision_of:
            raise ValueError("revision_of is required for a non-zero revision_number")
        if self.revision_of is not None:
            _text(self.revision_of, "revision_of")
            if self.revision_of == self.delta_id:
                raise ValueError("delta cannot revise itself")
        if not isinstance(self.canonical_event_digest, str) or len(self.canonical_event_digest) != 64:
            raise ValueError("canonical_event_digest must be a sha256 hex digest")
        try:
            int(self.canonical_event_digest, 16)
        except ValueError as exc:
            raise ValueError("canonical_event_digest must be a sha256 hex digest") from exc
        times = [
            _instant(self.source_observed_at, "source_observed_at"),
            _instant(self.collector_received_at, "collector_received_at"),
            _instant(self.collector_committed_at, "collector_committed_at"),
            _instant(self.desktop_available_at, "desktop_available_at"),
        ]
        if times != sorted(times):
            raise ValueError("collector causal timestamps cannot move backwards")
        if len(set(self.quality_flags)) != len(self.quality_flags) or any(
            not isinstance(flag, str) or not flag.strip() for flag in self.quality_flags
        ):
            raise ValueError("quality_flags must contain unique non-empty strings")
        if self.gap_state in {GapState.DETECTED, GapState.RECOVERED}:
            _text(self.gap_from_cursor, "gap_from_cursor")
            _text(self.gap_to_cursor, "gap_to_cursor")
        elif self.gap_from_cursor is not None or self.gap_to_cursor is not None:
            raise ValueError("gap cursors are only valid for DETECTED/RECOVERED")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        value = asdict(self)
        value["quality_flags"] = list(self.quality_flags)
        value["gap_state"] = self.gap_state.value
        return value

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CollectorDelta":
        value = dict(raw)
        value["quality_flags"] = tuple(value.get("quality_flags", ()))
        value["gap_state"] = GapState(value.get("gap_state", GapState.NONE))
        item = cls(**value)
        item.validate()
        return item


@dataclass(frozen=True, slots=True)
class StreamCheckpoint:
    source_id: str
    stream_epoch: str
    last_cursor: str
    last_position: int
    last_delta_id: str

    def validate(self) -> None:
        for name in ("source_id", "stream_epoch", "last_cursor", "last_delta_id"):
            _text(getattr(self, name), name)
        if isinstance(self.last_position, bool) or not isinstance(self.last_position, int) or self.last_position < 0:
            raise ValueError("last_position must be a non-negative int")


@dataclass(frozen=True, slots=True)
class DesktopAcknowledgement:
    delta_id: str
    canonical_event_digest: str
    acknowledged_at: str


class _JsonAtomicStore:
    schema_version = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._empty())
        self._read()

    def _empty(self) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    @staticmethod
    def _constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=self._pairs,
                parse_constant=self._constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid causal collector store") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != self.schema_version:
            raise ValueError("unsupported causal collector store schema")
        return raw

    def _write(self, raw: dict[str, Any]) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)


class CollectorDeltaStore(_JsonAtomicStore):
    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 1, "deltas": [], "streams": {}}

    def get(self, delta_id: str) -> CollectorDelta | None:
        _text(delta_id, "delta_id")
        for raw in self._read()["deltas"]:
            if raw.get("delta_id") == delta_id:
                return CollectorDelta.from_dict(raw)
        return None

    def _all(self) -> list[CollectorDelta]:
        return [CollectorDelta.from_dict(raw) for raw in self._read()["deltas"]]

    def append(self, delta: CollectorDelta) -> bool:
        delta.validate()
        raw = self._read()
        encoded = delta.to_dict()
        for existing in raw["deltas"]:
            if existing.get("delta_id") == delta.delta_id:
                if existing != encoded:
                    raise DeltaConflictError(f"delta {delta.delta_id} conflicts with immutable evidence")
                return False

        key = f"{delta.source_id}|{delta.stream_epoch}"
        previous_raw = raw["streams"].get(key)
        previous = None if previous_raw is None else StreamCheckpoint(**previous_raw)
        if previous is not None:
            previous.validate()
            if delta.cursor_position < previous.last_position:
                if delta.revision_of is None:
                    raise CursorRegressionError("source cursor moved backwards within one epoch")
                revised = next(
                    (item for item in raw["deltas"] if item.get("delta_id") == delta.revision_of), None
                )
                if revised is None or revised.get("cursor_position") != delta.cursor_position:
                    raise CursorRegressionError("revision must target an existing cursor position")
            if delta.cursor_position == previous.last_position and delta.revision_of is None:
                raise CursorRegressionError("equal cursor position requires an explicit revision relationship")

        raw["deltas"].append(encoded)
        if previous is None or delta.cursor_position > previous.last_position:
            checkpoint = StreamCheckpoint(
                delta.source_id, delta.stream_epoch, delta.source_cursor,
                delta.cursor_position, delta.delta_id
            )
            raw["streams"][key] = asdict(checkpoint)
        self._write(raw)
        return True

    def deltas_available_through(
        self, *, as_of: str, view: CausalView = CausalView.AS_KNOWN_AT_DECISION
    ) -> tuple[CollectorDelta, ...]:
        boundary = _instant(as_of, "as_of")
        items = [
            delta for delta in self._all()
            if _instant(delta.desktop_available_at, "desktop_available_at") <= boundary
        ]
        if view not in {CausalView.AS_KNOWN_AT_DECISION, CausalView.RESTATED_RESEARCH}:
            raise ValueError("unsupported causal view")
        return tuple(sorted(
            items,
            key=lambda item: (
                item.source_id, item.stream_epoch, item.cursor_position,
                item.revision_number, item.collector_committed_at, item.delta_id
            ),
        ))

    def stream_checkpoint(self, source_id: str, stream_epoch: str) -> StreamCheckpoint | None:
        raw = self._read()["streams"].get(f"{source_id}|{stream_epoch}")
        return None if raw is None else StreamCheckpoint(**raw)


class DesktopDeltaCheckpointStore(_JsonAtomicStore):
    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 1, "acks": [], "streams": {}}

    def has_ack(self, delta_id: str) -> bool:
        _text(delta_id, "delta_id")
        return any(item.get("delta_id") == delta_id for item in self._read()["acks"])

    def ack(self, delta: CollectorDelta, *, applied_event_digest: str, acknowledged_at: str) -> bool:
        delta.validate()
        if applied_event_digest != delta.canonical_event_digest:
            raise AckConflictError("desktop ack digest does not match collector evidence")
        _instant(acknowledged_at, "acknowledged_at")
        raw = self._read()
        existing = next((item for item in raw["acks"] if item.get("delta_id") == delta.delta_id), None)
        if existing is not None:
            if existing.get("canonical_event_digest") != applied_event_digest:
                raise AckConflictError("existing desktop ack disagrees with applied event")
            return False
        raw["acks"].append(asdict(DesktopAcknowledgement(
            delta.delta_id, applied_event_digest, acknowledged_at
        )))
        key = f"{delta.source_id}|{delta.stream_epoch}"
        previous_raw = raw["streams"].get(key)
        if previous_raw is None or delta.cursor_position > previous_raw["last_position"]:
            raw["streams"][key] = asdict(StreamCheckpoint(
                delta.source_id, delta.stream_epoch, delta.source_cursor,
                delta.cursor_position, delta.delta_id
            ))
        self._write(raw)
        return True

    def stream_checkpoint(self, source_id: str, stream_epoch: str) -> StreamCheckpoint | None:
        raw = self._read()["streams"].get(f"{source_id}|{stream_epoch}")
        return None if raw is None else StreamCheckpoint(**raw)


class DesktopDeltaConsumer:
    """Apply canonical event/health callbacks before the immutable desktop acknowledgement."""

    def __init__(
        self,
        collector: CollectorDeltaStore,
        checkpoint: DesktopDeltaCheckpointStore,
        *,
        resolve_event: Callable[[CollectorDelta], Any],
        apply_event: Callable[[Any], None],
        apply_health: Callable[[CollectorDelta, Any], None] | None = None,
    ) -> None:
        self.collector = collector
        self.checkpoint = checkpoint
        self.resolve_event = resolve_event
        self.apply_event = apply_event
        self.apply_health = apply_health

    def drain(self, *, as_of: str, view: CausalView = CausalView.AS_KNOWN_AT_DECISION) -> tuple[str, ...]:
        now = _instant(as_of, "as_of")
        available = self.collector.deltas_available_through(as_of=as_of, view=view)
        delivered: list[str] = []
        for delta in available:
            if self.checkpoint.has_ack(delta.delta_id):
                continue
            if delta.gap_state is GapState.DETECTED:
                recovered = any(
                    item.gap_state is GapState.RECOVERED and item.revision_of == delta.delta_id
                    for item in available
                )
                if recovered:
                    continue
                raise GapStateError(f"stream gap remains unresolved before delta {delta.delta_id}")
            event = self.resolve_event(delta)
            digest = canonical_event_digest(event)
            if digest != delta.canonical_event_digest:
                raise DeltaConflictError(f"canonical event digest mismatch for delta {delta.delta_id}")
            self.apply_event(event)
            if self.apply_health is not None:
                self.apply_health(delta, event)
            self.checkpoint.ack(
                delta,
                applied_event_digest=digest,
                acknowledged_at=now.isoformat(),
            )
            delivered.append(delta.delta_id)
        return tuple(delivered)


class RemoteCollectorAdapter:
    DEPLOYMENT_STATUS = "NOT_DEPLOYED"
    REQUIREMENT = "REQUIRES_EXTERNAL_INFRASTRUCTURE"

    def __init__(self, commit_delta: Callable[[CollectorDelta], bool]) -> None:
        self._commit_delta = commit_delta

    def submit_committed_delta(self, delta: CollectorDelta) -> bool:
        delta.validate()
        return self._commit_delta(delta)
