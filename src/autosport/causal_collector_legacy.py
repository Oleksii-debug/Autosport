from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping

from .workspace_lock import WorkspaceEconomicLock


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


class ApplicationReceiptError(CausalCollectorError):
    pass


class CausalView(StrEnum):
    AS_KNOWN_AT_DECISION = "AS_KNOWN_AT_DECISION"
    RESTATED_RESEARCH = "RESTATED_RESEARCH"


class GapState(StrEnum):
    NONE = "NONE"
    DETECTED = "DETECTED"
    RECOVERED = "RECOVERED"
    CURSOR_RESET = "CURSOR_RESET"


class SyncState(StrEnum):
    READY = "READY"
    GAP_DETECTED = "GAP_DETECTED"
    RECOVERED = "RECOVERED"
    CURSOR_RESET = "CURSOR_RESET"
    EPOCH_CHANGED = "EPOCH_CHANGED"
    RETRY_REQUIRED = "RETRY_REQUIRED"


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


def digest_source_payload(raw_payload: bytes | bytearray | memoryview | str) -> str:
    """Digest the exact provider/source payload bytes independently of normalization."""
    if isinstance(raw_payload, str):
        raw = raw_payload.encode("utf-8")
    elif isinstance(raw_payload, (bytes, bytearray, memoryview)):
        raw = bytes(raw_payload)
    else:
        raise TypeError("raw_payload must be bytes-like or str")
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
    source_payload_digest: str
    canonical_event_digest: str
    source_observed_at: str
    collector_received_at: str
    collector_committed_at: str
    desktop_available_at: str
    revision_of: str | None = None
    revision_number: int = 0
    quality_flags: tuple[str, ...] = ()
    gap_state: GapState = GapState.NONE
    sync_state: SyncState = SyncState.READY
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
        for field_name, digest in (
            ("source_payload_digest", self.source_payload_digest),
            ("canonical_event_digest", self.canonical_event_digest),
        ):
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError(f"{field_name} must be a sha256 hex digest")
            try:
                int(digest, 16)
            except ValueError as exc:
                raise ValueError(f"{field_name} must be a sha256 hex digest") from exc
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
        if not isinstance(self.sync_state, SyncState):
            try:
                SyncState(self.sync_state)
            except ValueError as exc:
                raise ValueError("unsupported sync_state") from exc
        expected_sync_state = {
            GapState.NONE: {SyncState.READY, SyncState.EPOCH_CHANGED, SyncState.RETRY_REQUIRED},
            GapState.DETECTED: {SyncState.GAP_DETECTED},
            GapState.RECOVERED: {SyncState.RECOVERED},
            GapState.CURSOR_RESET: {SyncState.CURSOR_RESET, SyncState.EPOCH_CHANGED},
        }[self.gap_state]
        if self.sync_state not in expected_sync_state:
            raise ValueError("sync_state does not match gap_state")
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
        value["sync_state"] = SyncState(value.get("sync_state", SyncState.READY))
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


@dataclass(frozen=True, slots=True)
class DesktopApplicationReceipt:
    delta_id: str
    canonical_event_digest: str
    receipt_id: str
    applied_at: str

    def validate(self) -> None:
        _text(self.delta_id, "delta_id")
        if not isinstance(self.canonical_event_digest, str) or len(self.canonical_event_digest) != 64:
            raise ApplicationReceiptError("application receipt digest must be a sha256 hex digest")
        try:
            int(self.canonical_event_digest, 16)
        except ValueError as exc:
            raise ApplicationReceiptError("application receipt digest must be a sha256 hex digest") from exc
        _text(self.receipt_id, "receipt_id")
        _instant(self.applied_at, "applied_at")


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


class _CanonicalDesktopApplicationStore(_JsonAtomicStore):
    """Durable coordination evidence; market and health stores remain canonical truth."""

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 1, "applications": {}}

    @staticmethod
    def _health_payload(state: Any) -> dict[str, Any]:
        payload = asdict(state)
        payload["quality_flags"] = list(state.quality_flags)
        return payload

    @staticmethod
    def _health_state(payload: Mapping[str, Any]) -> Any:
        from .ingestion_health import SourceHealthState

        value = dict(payload)
        value["quality_flags"] = tuple(value.get("quality_flags", ()))
        return SourceHealthState(**value)

    def prepare(
        self,
        delta: CollectorDelta,
        *,
        prepared_at: str,
        health_before: Any,
        health_after: Any,
    ) -> dict[str, Any]:
        delta.validate()
        _instant(prepared_at, "prepared_at")
        raw = self._read()
        existing = raw["applications"].get(delta.delta_id)
        immutable = {
            "delta_id": delta.delta_id,
            "canonical_event_digest": delta.canonical_event_digest,
            "source_id": delta.source_id,
            "source_cursor": delta.source_cursor,
            "prepared_at": prepared_at,
            "health_before": self._health_payload(health_before),
            "health_after": self._health_payload(health_after),
            "receipt_id": f"canonical-desktop:{delta.delta_id}:{delta.canonical_event_digest[:16]}",
        }
        if existing is not None:
            for key, value in immutable.items():
                if existing.get(key) != value:
                    raise ApplicationReceiptError(
                        f"canonical application progress conflicts on {key}"
                    )
            return existing
        item = {
            **immutable,
            "market_applied": False,
            "health_applied": False,
            "completed_at": None,
        }
        raw["applications"][delta.delta_id] = item
        self._write(raw)
        return item

    def progress(self, delta: CollectorDelta) -> dict[str, Any] | None:
        item = self._read()["applications"].get(delta.delta_id)
        if item is None:
            return None
        if item.get("canonical_event_digest") != delta.canonical_event_digest:
            raise ApplicationReceiptError("canonical application digest conflicts with delta")
        return item

    def _mark(self, delta: CollectorDelta, field: str) -> None:
        raw = self._read()
        item = raw["applications"].get(delta.delta_id)
        if item is None:
            raise ApplicationReceiptError("canonical application was not prepared")
        if item.get("canonical_event_digest") != delta.canonical_event_digest:
            raise ApplicationReceiptError("canonical application digest conflicts with delta")
        if item.get(field) is True:
            return
        item[field] = True
        self._write(raw)

    def mark_market_applied(self, delta: CollectorDelta) -> None:
        self._mark(delta, "market_applied")

    def mark_health_applied(self, delta: CollectorDelta) -> None:
        self._mark(delta, "health_applied")

    def mark_complete(self, delta: CollectorDelta, *, completed_at: str) -> str:
        raw = self._read()
        item = raw["applications"].get(delta.delta_id)
        if item is None:
            raise ApplicationReceiptError("canonical application was not prepared")
        if item.get("canonical_event_digest") != delta.canonical_event_digest:
            raise ApplicationReceiptError("canonical application digest conflicts with delta")
        if not item.get("market_applied") or not item.get("health_applied"):
            raise ApplicationReceiptError(
                "canonical application cannot complete before market and health are durable"
            )
        completed = _instant(completed_at, "completed_at")
        prepared = _instant(item["prepared_at"], "prepared_at")
        available = _instant(delta.desktop_available_at, "desktop_available_at")
        if completed < prepared or completed < available:
            raise ApplicationReceiptError(
                "canonical completion cannot predate preparation or desktop availability"
            )
        existing = item.get("completed_at")
        if existing is not None:
            _instant(existing, "completed_at")
            return existing
        item["completed_at"] = completed_at
        self._write(raw)
        return completed_at

    def health_before(self, delta: CollectorDelta) -> Any:
        item = self.progress(delta)
        if item is None:
            raise ApplicationReceiptError("canonical application was not prepared")
        return self._health_state(item["health_before"])

    def health_after(self, delta: CollectorDelta) -> Any:
        item = self.progress(delta)
        if item is None:
            raise ApplicationReceiptError("canonical application was not prepared")
        return self._health_state(item["health_after"])

    def receipt(self, delta: CollectorDelta) -> DesktopApplicationReceipt | None:
        item = self.progress(delta)
        if (
            item is None
            or not item.get("market_applied")
            or not item.get("health_applied")
            or item.get("completed_at") is None
        ):
            return None
        receipt = DesktopApplicationReceipt(
            delta_id=delta.delta_id,
            canonical_event_digest=delta.canonical_event_digest,
            receipt_id=item["receipt_id"],
            applied_at=item["completed_at"],
        )
        receipt.validate()
        return receipt


class CanonicalDesktopApplication:
    """Compose existing market and health authorities into one durable application receipt."""

    def __init__(
        self,
        market_bus: Any,
        health_store: Any,
        state_path: str | Path,
        *,
        clock: Callable[[], str],
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.market_bus = market_bus
        self.health_store = health_store
        self.clock = clock
        self._state = _CanonicalDesktopApplicationStore(state_path)

    def lookup_receipt(self, delta: CollectorDelta) -> DesktopApplicationReceipt | None:
        return self._state.receipt(delta)

    @staticmethod
    def _outcome(
        delta: CollectorDelta,
        event: Any,
        *,
        applied_at: str,
        health_before: Any,
    ) -> Any:
        from .ingestion import CommittedIngestionOutcome, _SourceHealthSnapshot

        return CommittedIngestionOutcome(
            source_id=delta.source_id,
            now=applied_at,
            received=1,
            accepted=1,
            rejected=0,
            elapsed_seconds=1.0,
            cursor=delta.source_cursor,
            latest_source_ts=getattr(event, "source_ts", None),
            quality_flags=tuple(sorted(delta.quality_flags)),
            health_before=_SourceHealthSnapshot.from_state(health_before),
        )

    def apply(self, delta: CollectorDelta, event: Any) -> DesktopApplicationReceipt:
        delta.validate()
        if getattr(event, "source_id", None) != delta.source_id:
            raise DeltaConflictError("canonical market event source_id conflicts with collector delta")
        if getattr(event, "event_id", None) != delta.event_id:
            raise DeltaConflictError("canonical market event event_id conflicts with collector delta")
        if getattr(event, "dedupe_key", None) != delta.event_dedupe_key:
            raise DeltaConflictError("canonical market event dedupe identity conflicts with collector delta")
        digest = canonical_event_digest(event)
        if digest != delta.canonical_event_digest:
            raise DeltaConflictError("canonical market event digest conflicts with collector delta")

        progress = self._state.progress(delta)
        if progress is None:
            prepared_at = self.clock()
            prepared = _instant(prepared_at, "prepared_at")
            if prepared < _instant(delta.desktop_available_at, "desktop_available_at"):
                raise ApplicationReceiptError(
                    "canonical application cannot predate desktop availability"
                )
            health_before = self.health_store.get(delta.source_id)
            outcome = self._outcome(
                delta,
                event,
                applied_at=prepared_at,
                health_before=health_before,
            )
            health_after = outcome.health_before.after_success(outcome).to_state()
            progress = self._state.prepare(
                delta,
                prepared_at=prepared_at,
                health_before=health_before,
                health_after=health_after,
            )

        if not progress.get("market_applied"):
            self.market_bus.publish(event)
            self._state.mark_market_applied(delta)
            progress = self._state.progress(delta)
            if progress is None:
                raise ApplicationReceiptError("canonical application progress disappeared")

        if not progress.get("health_applied"):
            expected_before = self._state.health_before(delta)
            expected_after = self._state.health_after(delta)
            current = self.health_store.get(delta.source_id)
            if current == expected_after:
                self._state.mark_health_applied(delta)
            elif current == expected_before:
                outcome = self._outcome(
                    delta,
                    event,
                    applied_at=progress["prepared_at"],
                    health_before=expected_before,
                )
                recorded = outcome.record_health(self.health_store)
                if recorded != expected_after:
                    raise ApplicationReceiptError(
                        "canonical health authority returned an unexpected post-state"
                    )
                self._state.mark_health_applied(delta)
            else:
                raise ApplicationReceiptError(
                    "canonical source health changed during desktop application; refusing ambiguous retry"
                )

        self._state.mark_complete(delta, completed_at=self.clock())
        receipt = self._state.receipt(delta)
        if receipt is None:
            raise ApplicationReceiptError("canonical application did not reach durable completion")
        return receipt


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
        prior_epochs = {
            stream_key.split("|", 1)[1]
            for stream_key in raw["streams"]
            if stream_key.startswith(f"{delta.source_id}|")
        }
        if prior_epochs and delta.stream_epoch not in prior_epochs and delta.sync_state not in {
            SyncState.EPOCH_CHANGED, SyncState.CURSOR_RESET
        }:
            raise CursorRegressionError("new stream epoch requires explicit epoch-change/reset state")
        previous = None if previous_raw is None else StreamCheckpoint(**previous_raw)
        if delta.revision_of is not None:
            revised = next(
                (item for item in raw["deltas"] if item.get("delta_id") == delta.revision_of), None
            )
            if revised is None:
                raise CursorRegressionError("revision must target an existing predecessor")
            predecessor = CollectorDelta.from_dict(revised)
            if predecessor.source_id != delta.source_id:
                raise CursorRegressionError("revision source_id does not match predecessor")
            if predecessor.stream_epoch != delta.stream_epoch:
                raise CursorRegressionError("revision stream_epoch does not match predecessor")
            if predecessor.event_dedupe_key != delta.event_dedupe_key:
                raise CursorRegressionError("revision event_dedupe_key does not match predecessor")
            if predecessor.event_id != delta.event_id:
                raise CursorRegressionError("revision event_id does not match predecessor")
            if predecessor.cursor_position != delta.cursor_position:
                raise CursorRegressionError("revision cursor_position does not match predecessor")
            if predecessor.source_cursor != delta.source_cursor:
                raise CursorRegressionError("revision source_cursor does not match predecessor")
            if delta.revision_number != predecessor.revision_number + 1:
                raise CursorRegressionError("revision_number must advance exactly one step")
            if delta.gap_from_cursor != predecessor.gap_from_cursor or delta.gap_to_cursor != predecessor.gap_to_cursor:
                raise GapStateError("revision gap bounds must match predecessor")
            if predecessor.gap_state is GapState.DETECTED:
                if delta.gap_state is not GapState.RECOVERED:
                    raise GapStateError("detected gap can only be revised by a recovered marker")
            elif delta.gap_state is GapState.RECOVERED:
                raise GapStateError("recovered marker must revise a detected gap")
        if previous is not None:
            previous.validate()
            if delta.cursor_position < previous.last_position and delta.revision_of is None:
                raise CursorRegressionError("source cursor moved backwards within one epoch")
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
        try:
            normalized_view = CausalView(view)
        except ValueError as exc:
            raise ValueError("unsupported causal view") from exc
        ordered = tuple(sorted(
            items,
            key=lambda item: (
                item.source_id, item.stream_epoch, item.cursor_position,
                item.revision_number, item.collector_committed_at, item.delta_id
            ),
        ))
        if normalized_view is CausalView.AS_KNOWN_AT_DECISION:
            # Preserve the actual evidence stream available by the historical cutoff.
            # A later correction appears only after its own desktop_available_at.
            return ordered
        # Research restatement projects the same cutoff onto the latest available
        # revision, while retaining revision_of/revision_number/availability evidence.
        superseded = {
            item.revision_of
            for item in ordered
            if item.revision_of is not None
        }
        return tuple(item for item in ordered if item.delta_id not in superseded)

    def deltas_after_commit(
        self,
        *,
        source_id: str,
        after_delta_id: str | None = None,
        max_items: int = 1000,
    ) -> tuple[CollectorDelta, ...]:
        """Return one bounded source feed in durable append/commit order.

        This transport cursor deliberately uses immutable delta identity rather than
        source cursor position. A correction may be committed later for an older
        source position; append order ensures a desktop that already consumed newer
        source positions still receives that correction instead of silently skipping it.
        """
        _text(source_id, "source_id")
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items <= 0
        ):
            raise ValueError("max_items must be a positive integer")
        items = [
            CollectorDelta.from_dict(raw)
            for raw in self._read()["deltas"]
            if raw.get("source_id") == source_id
        ]
        start = 0
        if after_delta_id is not None:
            _text(after_delta_id, "after_delta_id")
            for index, item in enumerate(items):
                if item.delta_id == after_delta_id:
                    start = index + 1
                    break
            else:
                raise CursorRegressionError(
                    "delivery cursor delta is not present for this source"
                )
        return tuple(items[start : start + max_items])

    def stream_checkpoint(self, source_id: str, stream_epoch: str) -> StreamCheckpoint | None:
        raw = self._read()["streams"].get(f"{source_id}|{stream_epoch}")
        return None if raw is None else StreamCheckpoint(**raw)


class DesktopDeltaCheckpointStore(_JsonAtomicStore):
    def __init__(self, path: str | Path) -> None:
        # First-open publication is part of the same shared checkpoint authority.
        # Without this double-check, two fresh processes can both observe "missing"
        # and a delayed empty initializer can replace a peer's first durable ACK.
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._read()
            return
        with self._workspace_lock():
            if not self.path.exists():
                self._write(self._empty())
            self._read()

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 1, "acks": [], "streams": {}}

    def _workspace_lock(self) -> WorkspaceEconomicLock:
        """Serialize checkpoint read/modify/write and application handoff across processes."""

        return WorkspaceEconomicLock(self.path.parent)

    def has_ack(self, delta_id: str) -> bool:
        _text(delta_id, "delta_id")
        return any(item.get("delta_id") == delta_id for item in self._read()["acks"])

    def application_receipt(self, delta: CollectorDelta) -> DesktopApplicationReceipt | None:
        for item in self._read()["acks"]:
            if item.get("delta_id") != delta.delta_id:
                continue
            receipt_id = item.get("application_receipt_id")
            if not receipt_id:
                continue
            receipt = DesktopApplicationReceipt(
                delta_id=item["delta_id"],
                canonical_event_digest=item["canonical_event_digest"],
                receipt_id=receipt_id,
                applied_at=item["applied_at"],
            )
            receipt.validate()
            return receipt
        return None

    def ack(self, delta: CollectorDelta, *, application_receipt: DesktopApplicationReceipt, acknowledged_at: str) -> bool:
        with self._workspace_lock():
            return self._ack_locked(
                delta,
                application_receipt=application_receipt,
                acknowledged_at=acknowledged_at,
            )

    def _ack_locked(
        self,
        delta: CollectorDelta,
        *,
        application_receipt: DesktopApplicationReceipt,
        acknowledged_at: str,
    ) -> bool:
        delta.validate()
        application_receipt.validate()
        if application_receipt.delta_id != delta.delta_id:
            raise ApplicationReceiptError("application receipt delta_id does not match collector evidence")
        if application_receipt.canonical_event_digest != delta.canonical_event_digest:
            raise ApplicationReceiptError("application receipt digest does not match collector evidence")
        desktop_available = _instant(delta.desktop_available_at, "desktop_available_at")
        applied = _instant(application_receipt.applied_at, "applied_at")
        acknowledged = _instant(acknowledged_at, "acknowledged_at")
        if not (desktop_available <= applied <= acknowledged):
            raise ApplicationReceiptError(
                "application timing must satisfy desktop_available_at <= applied_at <= acknowledged_at"
            )
        raw = self._read()
        existing = next((item for item in raw["acks"] if item.get("delta_id") == delta.delta_id), None)
        if existing is not None:
            if existing.get("canonical_event_digest") != delta.canonical_event_digest:
                raise AckConflictError("existing desktop ack disagrees with applied event")
            if existing.get("application_receipt_id") != application_receipt.receipt_id:
                raise ApplicationReceiptError("existing desktop receipt disagrees with applied effect")
            return False
        raw["acks"].append({
            "delta_id": delta.delta_id,
            "canonical_event_digest": delta.canonical_event_digest,
            "acknowledged_at": acknowledged_at,
            "application_receipt_id": application_receipt.receipt_id,
            "applied_at": application_receipt.applied_at,
        })
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
    """Apply one durable canonical event+health effect, then acknowledge the delta.

    ``apply_event`` is the complete idempotent application boundary and must not return a
    receipt until both canonical market persistence and required source-health persistence
    are durable. ``lookup_application_receipt`` recovers only such complete receipts.
    Separate post-receipt health mutation is rejected because it creates an unrecoverable
    crash boundary between event persistence and desktop acknowledgement.
    """

    def __init__(
        self,
        collector: CollectorDeltaStore,
        checkpoint: DesktopDeltaCheckpointStore,
        *,
        resolve_event: Callable[[CollectorDelta], Any],
        apply_event: Callable[[CollectorDelta, Any], DesktopApplicationReceipt],
        lookup_application_receipt: Callable[[CollectorDelta], DesktopApplicationReceipt | None],
        apply_health: Callable[[CollectorDelta, Any], None] | None = None,
    ) -> None:
        self.collector = collector
        self.checkpoint = checkpoint
        self.resolve_event = resolve_event
        self.apply_event = apply_event
        self.lookup_application_receipt = lookup_application_receipt
        if apply_health is not None:
            raise ApplicationReceiptError(
                "apply_health must be included inside the durable apply_event boundary"
            )

    def _contiguous_available_deltas(
        self,
        *,
        available: tuple[CollectorDelta, ...],
        as_of: datetime,
    ) -> tuple[CollectorDelta, ...]:
        """Project visible deltas in durable transport order without crossing a causal gap.

        deltas_after_commit is the canonical desktop transport order. The causal
        availability projection can be cursor-sorted and can expose a later commit
        before an earlier commit becomes desktop-visible. Walk the durable source
        feed instead: a not-yet-visible row fences only later commits in its own
        epoch, while rows already committed before that hidden row remain eligible.
        Revisions therefore preserve their actual commit position.
        """

        if not available:
            return ()

        available_by_id = {delta.delta_id: delta for delta in available}
        if len(available_by_id) != len(available):
            raise DeltaConflictError(
                "desktop availability projection contains duplicate delta_id"
            )

        expected_ids = set(available_by_id)
        accounted_ids: set[str] = set()
        ordered: list[CollectorDelta] = []
        page_size = 1000

        for source_id in sorted({delta.source_id for delta in available}):
            after_delta_id: str | None = None
            hidden_epoch_rows: set[str] = set()

            while True:
                page = self.collector.deltas_after_commit(
                    source_id=source_id,
                    after_delta_id=after_delta_id,
                    max_items=page_size,
                )
                if not page:
                    break

                for committed in page:
                    selected = available_by_id.get(committed.delta_id)
                    if selected is not None:
                        accounted_ids.add(committed.delta_id)

                    if _instant(
                        committed.desktop_available_at,
                        "desktop_available_at",
                    ) > as_of:
                        hidden_epoch_rows.add(committed.stream_epoch)
                        continue
                    if committed.stream_epoch in hidden_epoch_rows:
                        continue
                    if selected is not None:
                        ordered.append(selected)

                after_delta_id = page[-1].delta_id
                if len(page) < page_size:
                    break

        if accounted_ids != expected_ids:
            raise CursorRegressionError(
                "desktop availability projection is not present in durable commit order"
            )

        return tuple(ordered)

    def drain(self, *, as_of: str, view: CausalView = CausalView.AS_KNOWN_AT_DECISION) -> tuple[str, ...]:
        now = _instant(as_of, "as_of")
        available = self.collector.deltas_available_through(as_of=as_of, view=view)
        contiguous_available = self._contiguous_available_deltas(
            available=available,
            as_of=now,
        )
        delivered: list[str] = []
        for delta in contiguous_available:
            if delta.gap_state is GapState.DETECTED:
                recovered = any(
                    item.gap_state is GapState.RECOVERED
                    and item.revision_of == delta.delta_id
                    and item.source_id == delta.source_id
                    and item.stream_epoch == delta.stream_epoch
                    and item.event_dedupe_key == delta.event_dedupe_key
                    and item.event_id == delta.event_id
                    and item.revision_number == delta.revision_number + 1
                    and item.gap_from_cursor == delta.gap_from_cursor
                    and item.gap_to_cursor == delta.gap_to_cursor
                    for item in available
                )
                if recovered:
                    continue
                raise GapStateError(f"stream gap remains unresolved before delta {delta.delta_id}")

            # The lock covers the complete exactly-once desktop handoff. Every process
            # re-reads acknowledgement and durable receipt state after acquisition,
            # then either adopts that completed evidence or owns apply+ack atomically
            # with respect to all cooperating Autosport desktop consumers.
            with self.checkpoint._workspace_lock():
                if self.checkpoint.has_ack(delta.delta_id):
                    continue

                durable_receipt = self.lookup_application_receipt(delta)
                if durable_receipt is not None:
                    durable_receipt.validate()
                    if durable_receipt.canonical_event_digest != delta.canonical_event_digest:
                        raise ApplicationReceiptError(
                            f"durable application receipt digest conflicts with delta {delta.delta_id}"
                        )
                    self.checkpoint._ack_locked(
                        delta,
                        application_receipt=durable_receipt,
                        acknowledged_at=now.isoformat(),
                    )
                    delivered.append(delta.delta_id)
                    continue

                event = self.resolve_event(delta)
                digest = canonical_event_digest(event)
                if digest != delta.canonical_event_digest:
                    raise DeltaConflictError(f"canonical event digest mismatch for delta {delta.delta_id}")
                receipt = self.apply_event(delta, event)
                if not isinstance(receipt, DesktopApplicationReceipt):
                    raise ApplicationReceiptError("apply_event must return a durable DesktopApplicationReceipt")
                receipt.validate()
                if receipt.delta_id != delta.delta_id or receipt.canonical_event_digest != digest:
                    raise ApplicationReceiptError("application receipt is not bound to this delta/digest")
                self.checkpoint._ack_locked(
                    delta,
                    application_receipt=receipt,
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
