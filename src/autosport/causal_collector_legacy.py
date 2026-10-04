from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping

from .domain import MarketEvent
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


def _canonical_event_digest_impl(
    event_or_payload: Any,
    *,
    _market_event_type,
    _market_event_to_dict,
    _dumps,
    _sha256,
) -> str:
    if type(event_or_payload) is _market_event_type:
        payload = _market_event_to_dict(event_or_payload)
    else:
        payload = (
            event_or_payload.to_dict()
            if hasattr(event_or_payload, "to_dict")
            else event_or_payload
        )
    try:
        raw = _dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("canonical event payload is not JSON-safe") from exc
    return _sha256(raw).hexdigest()


def _bind_canonical_event_digest(implementation):
    """Keep canonical digest roots outside caller-writable function defaults."""

    market_event_type = MarketEvent
    market_event_to_dict = MarketEvent.to_dict
    dumps = json.dumps
    sha256 = hashlib.sha256

    def canonical_event_digest(event_or_payload: Any) -> str:
        """Hash canonical event evidence through composition-time authority roots."""

        return implementation(
            event_or_payload,
            _market_event_type=market_event_type,
            _market_event_to_dict=market_event_to_dict,
            _dumps=dumps,
            _sha256=sha256,
        )

    return canonical_event_digest


canonical_event_digest = _bind_canonical_event_digest(_canonical_event_digest_impl)
del _canonical_event_digest_impl
del _bind_canonical_event_digest


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

    def __init__(self, path: str | Path) -> None:
        # Completed application receipts are now restart decision authority. Publish
        # the initial empty file under the same cross-process economic lock used by
        # the desktop apply+ack handoff so a delayed first opener cannot overwrite a
        # peer's first completed receipt after both observed the path as missing.
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._read()
            return
        with WorkspaceEconomicLock(self.path.parent):
            if not self.path.exists():
                self._write(self._empty())
            self._read()

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
        delta.validate()
        raw = self._read()
        applications = raw.get("applications")
        if type(applications) is not dict:
            raise ApplicationReceiptError(
                "canonical desktop application index is malformed"
            )
        item = applications.get(delta.delta_id)
        if item is None:
            return None
        if type(item) is not dict:
            raise ApplicationReceiptError(
                "canonical desktop application entry is malformed"
            )
        expected_receipt_id = (
            f"canonical-desktop:{delta.delta_id}:{delta.canonical_event_digest[:16]}"
        )
        expected_identity = {
            "delta_id": delta.delta_id,
            "canonical_event_digest": delta.canonical_event_digest,
            "source_id": delta.source_id,
            "source_cursor": delta.source_cursor,
            "receipt_id": expected_receipt_id,
        }
        for field_name, expected in expected_identity.items():
            if item.get(field_name) != expected:
                raise ApplicationReceiptError(
                    f"canonical application progress conflicts on {field_name}"
                )
        try:
            prepared = _instant(item.get("prepared_at"), "prepared_at")
            available = _instant(
                delta.desktop_available_at,
                "desktop_available_at",
            )
            health_before = self._health_state(item.get("health_before"))
            health_after = self._health_state(item.get("health_after"))
        except (TypeError, ValueError) as exc:
            raise ApplicationReceiptError(
                "canonical application progress is malformed"
            ) from exc
        if prepared < available:
            raise ApplicationReceiptError(
                "canonical application preparation predates desktop availability"
            )
        if (
            health_before.source_id != delta.source_id
            or health_after.source_id != delta.source_id
        ):
            raise ApplicationReceiptError(
                "canonical application health source identity conflicts with delta"
            )
        market_applied = item.get("market_applied")
        health_applied = item.get("health_applied")
        if type(market_applied) is not bool or type(health_applied) is not bool:
            raise ApplicationReceiptError(
                "canonical application completion flags are invalid"
            )
        if health_applied and not market_applied:
            raise ApplicationReceiptError(
                "canonical application health cannot precede market persistence"
            )
        completed_at = item.get("completed_at")
        if completed_at is not None:
            if not market_applied or not health_applied:
                raise ApplicationReceiptError(
                    "canonical application completed without durable effects"
                )
            receipt = self._validated_completed_receipt(
                delta_id=delta.delta_id,
                item=item,
                stored_source_id=delta.source_id,
            )
            if _instant(receipt.applied_at, "completed_at") < available:
                raise ApplicationReceiptError(
                    "canonical application completion predates desktop availability"
                )
        return item

    def _mark(self, delta: CollectorDelta, field: str) -> None:
        if self.progress(delta) is None:
            raise ApplicationReceiptError("canonical application was not prepared")
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
        if self.progress(delta) is None:
            raise ApplicationReceiptError("canonical application was not prepared")
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

    def _validated_completed_receipt(
        self,
        *,
        delta_id: str,
        item: Mapping[str, Any],
        stored_source_id: str,
    ) -> DesktopApplicationReceipt:
        """Re-prove restart-authority evidence without compacted CollectorDelta rows."""

        try:
            source_cursor = _text(item.get("source_cursor"), "source_cursor")
            prepared_at = item.get("prepared_at")
            prepared = _instant(prepared_at, "prepared_at")
            completed_at = item.get("completed_at")
            completed = _instant(completed_at, "completed_at")
            health_before = self._health_state(item.get("health_before"))
            health_after = self._health_state(item.get("health_after"))
            receipt = DesktopApplicationReceipt(
                delta_id=delta_id,
                canonical_event_digest=item.get("canonical_event_digest"),
                receipt_id=item.get("receipt_id"),
                applied_at=completed_at,
            )
            receipt.validate()
        except (TypeError, ValueError) as exc:
            raise ApplicationReceiptError(
                "completed canonical desktop application evidence is malformed"
            ) from exc

        expected_receipt_id = (
            f"canonical-desktop:{delta_id}:{receipt.canonical_event_digest[:16]}"
        )
        if receipt.receipt_id != expected_receipt_id:
            raise ApplicationReceiptError(
                "completed canonical desktop application receipt identity is invalid"
            )
        if completed < prepared:
            raise ApplicationReceiptError(
                "completed canonical desktop application predates preparation"
            )
        if (
            health_before.source_id != stored_source_id
            or health_after.source_id != stored_source_id
        ):
            raise ApplicationReceiptError(
                "completed canonical desktop application health source identity changed"
            )

        expected_after = {
            "poll_count": health_before.poll_count + 1,
            "total_received": health_before.total_received + 1,
            "total_accepted": health_before.total_accepted + 1,
            "total_rejected": health_before.total_rejected,
            "total_failures": health_before.total_failures,
            "consecutive_failures": 0,
            "last_success_at": prepared_at,
            "last_error_at": health_before.last_error_at,
            "last_error": None,
            "last_cursor": source_cursor,
            "last_failure_kind": None,
            "consecutive_failure_kind_count": 0,
        }
        for field_name, expected in expected_after.items():
            if getattr(health_after, field_name) != expected:
                raise ApplicationReceiptError(
                    "completed canonical desktop application health transition is invalid"
                )

        expected_status = "degraded" if health_after.quality_flags else "healthy"
        if health_after.status != expected_status:
            raise ApplicationReceiptError(
                "completed canonical desktop application health status is invalid"
            )
        if health_after.quality_flags != tuple(sorted(health_after.quality_flags)):
            raise ApplicationReceiptError(
                "completed canonical desktop application quality flags are not canonical"
            )
        if health_before.latest_source_ts is not None:
            if health_after.latest_source_ts is None or _instant(
                health_after.latest_source_ts,
                "health_after.latest_source_ts",
            ) < _instant(
                health_before.latest_source_ts,
                "health_before.latest_source_ts",
            ):
                raise ApplicationReceiptError(
                    "completed canonical desktop application source time regressed"
                )
        return receipt

    def completed_receipts_for_source(
        self,
        source_id: str,
    ) -> tuple[DesktopApplicationReceipt, ...]:
        """Return completed product-owned application receipts for one source.

        This index is intentionally independent of retained collector rows: acknowledged
        deltas from old epochs may be compacted, while the completed desktop application
        remains the durable witness that a canonical market effect reached the product.
        """

        _text(source_id, "source_id")
        raw = self._read()
        applications = raw.get("applications")
        if type(applications) is not dict:
            raise ApplicationReceiptError(
                "canonical desktop application index is malformed"
            )
        receipts: list[DesktopApplicationReceipt] = []
        for delta_id, item in applications.items():
            if type(delta_id) is not str or type(item) is not dict:
                raise ApplicationReceiptError(
                    "canonical desktop application entry is malformed"
                )
            if item.get("delta_id") != delta_id:
                raise ApplicationReceiptError(
                    "canonical desktop application delta identity is inconsistent"
                )
            stored_source_id = item.get("source_id")
            try:
                _text(stored_source_id, "source_id")
            except (TypeError, ValueError) as exc:
                raise ApplicationReceiptError(
                    "canonical desktop application source identity is invalid"
                ) from exc
            if stored_source_id != source_id:
                continue
            market_applied = item.get("market_applied")
            health_applied = item.get("health_applied")
            if type(market_applied) is not bool or type(health_applied) is not bool:
                raise ApplicationReceiptError(
                    "canonical desktop application completion flags are invalid"
                )
            completed_at = item.get("completed_at")
            if completed_at is None:
                continue
            if not market_applied or not health_applied:
                raise ApplicationReceiptError(
                    "canonical desktop application completed without durable effects"
                )
            receipts.append(
                self._validated_completed_receipt(
                    delta_id=delta_id,
                    item=item,
                    stored_source_id=stored_source_id,
                )
            )
        return tuple(sorted(receipts, key=lambda receipt: receipt.delta_id))


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

    def _lookup_receipt_impl(
        self,
        delta: CollectorDelta,
        *,
        _market_bus_type,
        _market_store_type,
        _market_events,
        _event_type,
        _canonical_digest,
        _dedupe_getter,
        _health_store_type,
        _health_get,
        _application_store_type,
        _state_receipt,
        _state_health_after,
    ) -> DesktopApplicationReceipt | None:
        if type(self._state) is not _application_store_type:
            raise ApplicationReceiptError(
                "completed canonical application lacks canonical journal authority"
            )
        receipt = _state_receipt(state, delta)
        if receipt is None:
            return None

        if type(self.market_bus) is not _market_bus_type:
            raise ApplicationReceiptError(
                "completed canonical application lacks canonical market bus authority"
            )
        market_store = getattr(market_bus, "store", None)
        if type(market_store) is not _market_store_type:
            raise ApplicationReceiptError(
                "completed canonical application lacks canonical market storage authority"
            )
        try:
            history = _market_events(market_store, delta.event_id)
        except Exception as exc:
            raise ApplicationReceiptError(
                "cannot verify durable canonical market effect for application receipt"
            ) from exc

        matches = []
        for event in history:
            if type(event) is not _event_type:
                raise ApplicationReceiptError(
                    "canonical market history returned a non-canonical event type"
                )
            try:
                event_dedupe_key = _dedupe_getter(event)
                event_digest = _canonical_digest(event)
            except Exception as exc:
                raise ApplicationReceiptError(
                    "cannot verify canonical market identity for application receipt"
                ) from exc
            if (
                event.source_id == delta.source_id
                and event_dedupe_key == delta.event_dedupe_key
                and event_digest == receipt.canonical_event_digest
            ):
                matches.append(event)
        if len(matches) != 1:
            raise ApplicationReceiptError(
                "application receipt does not resolve to exactly one durable canonical market effect"
            )

        if type(self.health_store) is not _health_store_type:
            raise ApplicationReceiptError(
                "completed canonical application lacks canonical health authority"
            )
        expected_health = _state_health_after(state, delta)
        try:
            actual_health = _health_get(self.health_store, delta.source_id)
        except Exception as exc:
            raise ApplicationReceiptError(
                "cannot verify durable canonical health effect for application receipt"
            ) from exc
        if actual_health != expected_health:
            raise ApplicationReceiptError(
                "application receipt lacks its durable canonical health effect"
            )
        return receipt

    def completed_receipts_for_source(
        self,
        source_id: str,
    ) -> tuple[DesktopApplicationReceipt, ...]:
        return self._state.completed_receipts_for_source(source_id)

    def _verified_completed_receipts_for_source_impl(
        self,
        source_id: str,
        *,
        _health_store_type,
        _health_read,
        _application_read,
        _application_store_type,
        _completed_receipts,
    ) -> tuple[DesktopApplicationReceipt, ...]:
        if type(self._state) is not _application_store_type:
            raise ApplicationReceiptError(
                "completed canonical applications lack canonical journal authority"
            )
        receipts = _completed_receipts(self._state, source_id)
        if not receipts:
            return ()

        if type(self.health_store) is not _health_store_type:
            raise ApplicationReceiptError(
                "completed canonical applications lack canonical health authority"
            )
        try:
            health_raw = _health_read(self.health_store)
            application_raw = _application_read(self._state)
        except Exception as exc:
            raise ApplicationReceiptError(
                "cannot verify completed canonical application health history"
            ) from exc

        history = health_raw.get("history")
        applications = application_raw.get("applications")
        if type(history) is not dict or type(applications) is not dict:
            raise ApplicationReceiptError(
                "completed canonical application health evidence is malformed"
            )
        entries = history.get(source_id, ())
        if type(entries) is not list:
            raise ApplicationReceiptError(
                "completed canonical application health history is malformed"
            )

        for receipt in receipts:
            item = applications.get(receipt.delta_id)
            if type(item) is not dict:
                raise ApplicationReceiptError(
                    "completed canonical application entry disappeared"
                )
            if (
                receipt.canonical_event_digest != item.get("canonical_event_digest")
                or receipt.receipt_id != item.get("receipt_id")
                or receipt.applied_at != item.get("completed_at")
            ):
                raise ApplicationReceiptError(
                    "completed canonical application receipt conflicts with journal evidence"
                )
            expected_health = item.get("health_after")
            if not any(
                type(entry) is dict and entry.get("state") == expected_health
                for entry in entries
            ):
                raise ApplicationReceiptError(
                    "completed canonical application lacks durable health history evidence"
                )
        return receipts

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

    def _apply_impl(
        self,
        delta: CollectorDelta,
        event: Any,
        *,
        _market_event_type,
        _canonical_digest,
        _market_bus_type,
        _market_publish,
        _market_store_type,
        _market_events,
        _dedupe_getter,
        _health_store_type,
        _health_get,
        _record_health,
        _outcome_builder,
        _application_store_type,
        _state_progress,
        _state_prepare,
        _state_health_before,
        _state_health_after,
        _state_mark_market_applied,
        _state_mark_health_applied,
        _state_mark_complete,
        _state_receipt,
    ) -> DesktopApplicationReceipt:
        delta.validate()
        state = self._state
        market_bus = self.market_bus
        health_store = self.health_store
        application_clock = self.clock
        if type(state) is not _application_store_type:
            raise ApplicationReceiptError(
                "canonical application lacks canonical journal authority"
            )
        if type(market_bus) is not _market_bus_type:
            raise ApplicationReceiptError(
                "canonical application lacks canonical market bus authority"
            )
        if type(health_store) is not _health_store_type:
            raise ApplicationReceiptError(
                "canonical application lacks canonical health authority"
            )
        if not callable(application_clock):
            raise ApplicationReceiptError(
                "canonical application clock authority is unavailable"
            )
        if type(event) is not _market_event_type:
            raise DeltaConflictError(
                "canonical desktop application requires an exact MarketEvent"
            )
        if getattr(event, "source_id", None) != delta.source_id:
            raise DeltaConflictError("canonical market event source_id conflicts with collector delta")
        if getattr(event, "event_id", None) != delta.event_id:
            raise DeltaConflictError("canonical market event event_id conflicts with collector delta")
        if getattr(event, "dedupe_key", None) != delta.event_dedupe_key:
            raise DeltaConflictError("canonical market event dedupe identity conflicts with collector delta")
        digest = _canonical_digest(event)
        if digest != delta.canonical_event_digest:
            raise DeltaConflictError("canonical market event digest conflicts with collector delta")

        progress = _state_progress(state, delta)
        if progress is None:
            prepared_at = application_clock()
            prepared = _instant(prepared_at, "prepared_at")
            if prepared < _instant(delta.desktop_available_at, "desktop_available_at"):
                raise ApplicationReceiptError(
                    "canonical application cannot predate desktop availability"
                )
            health_before = _health_get(health_store, delta.source_id)
            outcome = _outcome_builder(
                delta,
                event,
                applied_at=prepared_at,
                health_before=health_before,
            )
            health_after = outcome.health_before.after_success(outcome).to_state()
            progress = _state_prepare(state,
                delta,
                prepared_at=prepared_at,
                health_before=health_before,
                health_after=health_after,
            )

        expected_before = _state_health_before(state, delta)
        expected_after = _state_health_after(self._state, delta)
        reproved_outcome = _outcome_builder(
            delta,
            event,
            applied_at=progress["prepared_at"],
            health_before=expected_before,
        )
        reproved_after = (
            reproved_outcome.health_before.after_success(reproved_outcome).to_state()
        )
        if reproved_after != expected_after:
            raise ApplicationReceiptError(
                "canonical application health transition conflicts with event evidence"
            )

        if not progress.get("market_applied"):
            # MarketEvent is frozen only at the outer dataclass layer; canonical
            # metadata remains a mutable JSON object. External preparation callbacks
            # (notably the injected clock) run after the first digest proof, so repeat
            # the proof at the last boundary before durable market persistence.
            if _canonical_digest(event) != digest:
                raise DeltaConflictError(
                    "canonical market event changed during desktop application"
                )
            _market_publish(market_bus, event)
            if _canonical_digest(event) != digest:
                raise DeltaConflictError(
                    "canonical market event changed during market persistence"
                )
            market_store = getattr(market_bus, "store", None)
            if type(market_store) is not _market_store_type:
                raise ApplicationReceiptError(
                    "canonical application cannot prove durable market storage"
                )
            try:
                history = _market_events(market_store, delta.event_id)
            except Exception as exc:
                raise ApplicationReceiptError(
                    "cannot verify durable market effect after publication"
                ) from exc
            matches = []
            for stored_event in history:
                if type(stored_event) is not _market_event_type:
                    raise ApplicationReceiptError(
                        "market persistence returned a non-canonical event type"
                    )
                try:
                    stored_dedupe_key = _dedupe_getter(stored_event)
                    stored_digest = _canonical_digest(stored_event)
                except Exception as exc:
                    raise ApplicationReceiptError(
                        "cannot verify durable market identity after publication"
                    ) from exc
                if (
                    stored_event.source_id == delta.source_id
                    and stored_dedupe_key == delta.event_dedupe_key
                    and stored_digest == digest
                ):
                    matches.append(stored_event)
            if len(matches) != 1:
                raise ApplicationReceiptError(
                    "canonical application market effect is not durably provable"
                )
            _state_mark_market_applied(state, delta)
            progress = _state_progress(state, delta)
            if progress is None:
                raise ApplicationReceiptError("canonical application progress disappeared")

        if not progress.get("health_applied"):
            current = _health_get(health_store, delta.source_id)
            if current == expected_after:
                _state_mark_health_applied(state, delta)
            elif current == expected_before:
                recorded = _record_health(reproved_outcome, health_store)
                if recorded != expected_after:
                    raise ApplicationReceiptError(
                        "canonical health authority returned an unexpected post-state"
                    )
                _state_mark_health_applied(state, delta)
            else:
                raise ApplicationReceiptError(
                    "canonical source health changed during desktop application; refusing ambiguous retry"
                )
        else:
            current = _health_get(health_store, delta.source_id)
            if current != expected_after:
                raise ApplicationReceiptError(
                    "canonical application health marker lacks its durable post-state"
                )

        completed_at = application_clock()
        if _canonical_digest(event) != digest:
            raise DeltaConflictError(
                "canonical market event changed before application completion"
            )
        current = _health_get(health_store, delta.source_id)
        if current != expected_after:
            raise ApplicationReceiptError(
                "canonical health effect changed before application completion"
            )
        market_store = getattr(self.market_bus, "store", None)
        if type(market_store) is not _market_store_type:
            raise ApplicationReceiptError(
                "canonical application cannot reprove market storage before completion"
            )
        try:
            history = _market_events(market_store, delta.event_id)
        except Exception as exc:
            raise ApplicationReceiptError(
                "cannot reverify durable market effect before completion"
            ) from exc
        matches = []
        for stored_event in history:
            if type(stored_event) is not _market_event_type:
                raise ApplicationReceiptError(
                    "market persistence returned a non-canonical event type"
                )
            try:
                stored_dedupe_key = _dedupe_getter(stored_event)
                stored_digest = _canonical_digest(stored_event)
            except Exception as exc:
                raise ApplicationReceiptError(
                    "cannot reverify durable market identity before completion"
                ) from exc
            if (
                stored_event.source_id == delta.source_id
                and stored_dedupe_key == delta.event_dedupe_key
                and stored_digest == digest
            ):
                matches.append(stored_event)
        if len(matches) != 1:
            raise ApplicationReceiptError(
                "canonical market effect changed before application completion"
            )

        _state_mark_complete(state, delta, completed_at=completed_at)
        receipt = _state_receipt(self._state, delta)
        if receipt is None:
            raise ApplicationReceiptError("canonical application did not reach durable completion")
        return receipt


def _bind_canonical_desktop_application_apply(implementation):
    """Seal the exact market-event type and digest authority for desktop application."""

    from .ingestion import CommittedIngestionOutcome
    from .ingestion_health import SourceHealthStore
    from .market_bus import MarketEventBus
    from .storage import SQLiteMarketStore

    market_event_type = MarketEvent
    canonical_digest = canonical_event_digest
    market_bus_type = MarketEventBus
    market_publish = MarketEventBus.publish
    market_store_type = SQLiteMarketStore
    market_events = SQLiteMarketStore.events
    dedupe_getter = MarketEvent.dedupe_key.fget
    health_store_type = SourceHealthStore
    health_get = SourceHealthStore.get
    record_health = CommittedIngestionOutcome.record_health
    outcome_builder = CanonicalDesktopApplication._outcome
    application_store_type = _CanonicalDesktopApplicationStore
    state_progress = _CanonicalDesktopApplicationStore.progress
    state_prepare = _CanonicalDesktopApplicationStore.prepare
    state_health_before = _CanonicalDesktopApplicationStore.health_before
    state_health_after = _CanonicalDesktopApplicationStore.health_after
    state_mark_market_applied = _CanonicalDesktopApplicationStore.mark_market_applied
    state_mark_health_applied = _CanonicalDesktopApplicationStore.mark_health_applied
    state_mark_complete = _CanonicalDesktopApplicationStore.mark_complete
    state_receipt = _CanonicalDesktopApplicationStore.receipt

    def apply(
        self: CanonicalDesktopApplication,
        delta: CollectorDelta,
        event: Any,
    ) -> DesktopApplicationReceipt:
        return implementation(
            self,
            delta,
            event,
            _market_event_type=market_event_type,
            _canonical_digest=canonical_digest,
            _market_bus_type=market_bus_type,
            _market_publish=market_publish,
            _market_store_type=market_store_type,
            _market_events=market_events,
            _dedupe_getter=dedupe_getter,
            _health_store_type=health_store_type,
            _health_get=health_get,
            _record_health=record_health,
            _outcome_builder=outcome_builder,
            _application_store_type=application_store_type,
            _state_progress=state_progress,
            _state_prepare=state_prepare,
            _state_health_before=state_health_before,
            _state_health_after=state_health_after,
            _state_mark_market_applied=state_mark_market_applied,
            _state_mark_health_applied=state_mark_health_applied,
            _state_mark_complete=state_mark_complete,
            _state_receipt=state_receipt,
        )

    return apply


CanonicalDesktopApplication.apply = _bind_canonical_desktop_application_apply(
    CanonicalDesktopApplication._apply_impl
)
del CanonicalDesktopApplication._apply_impl
del _bind_canonical_desktop_application_apply


def _bind_canonical_desktop_application_lookup_receipt(implementation):
    """Re-prove completed receipt effects through sealed canonical authorities."""

    from .ingestion_health import SourceHealthStore
    from .market_bus import MarketEventBus
    from .storage import SQLiteMarketStore

    market_bus_type = MarketEventBus
    market_store_type = SQLiteMarketStore
    market_events = SQLiteMarketStore.events
    event_type = MarketEvent
    canonical_digest = canonical_event_digest
    dedupe_getter = MarketEvent.dedupe_key.fget
    health_store_type = SourceHealthStore
    health_get = SourceHealthStore.get
    application_store_type = _CanonicalDesktopApplicationStore
    state_receipt = _CanonicalDesktopApplicationStore.receipt
    state_health_after = _CanonicalDesktopApplicationStore.health_after

    def lookup_receipt(
        self: CanonicalDesktopApplication,
        delta: CollectorDelta,
    ) -> DesktopApplicationReceipt | None:
        if dedupe_getter is None:
            raise ApplicationReceiptError(
                "canonical MarketEvent dedupe identity descriptor is unavailable"
            )
        return implementation(
            self,
            delta,
            _market_bus_type=market_bus_type,
            _market_store_type=market_store_type,
            _market_events=market_events,
            _event_type=event_type,
            _canonical_digest=canonical_digest,
            _dedupe_getter=dedupe_getter,
            _health_store_type=health_store_type,
            _health_get=health_get,
            _application_store_type=application_store_type,
            _state_receipt=state_receipt,
            _state_health_after=state_health_after,
        )

    return lookup_receipt


CanonicalDesktopApplication.lookup_receipt = (
    _bind_canonical_desktop_application_lookup_receipt(
        CanonicalDesktopApplication._lookup_receipt_impl
    )
)
del CanonicalDesktopApplication._lookup_receipt_impl
del _bind_canonical_desktop_application_lookup_receipt


def _bind_verified_completed_receipts_for_source(implementation):
    """Bind restart health-history proof to canonical durable authorities."""

    from .ingestion_health import SourceHealthStore

    health_store_type = SourceHealthStore
    health_read = SourceHealthStore._read
    application_read = _JsonAtomicStore._read
    application_store_type = _CanonicalDesktopApplicationStore
    completed_receipts = _CanonicalDesktopApplicationStore.completed_receipts_for_source

    def verified_completed_receipts_for_source(
        self: CanonicalDesktopApplication,
        source_id: str,
    ) -> tuple[DesktopApplicationReceipt, ...]:
        return implementation(
            self,
            source_id,
            _health_store_type=health_store_type,
            _health_read=health_read,
            _application_read=application_read,
            _application_store_type=application_store_type,
            _completed_receipts=completed_receipts,
        )

    return verified_completed_receipts_for_source


CanonicalDesktopApplication.verified_completed_receipts_for_source = (
    _bind_verified_completed_receipts_for_source(
        CanonicalDesktopApplication._verified_completed_receipts_for_source_impl
    )
)
del CanonicalDesktopApplication._verified_completed_receipts_for_source_impl
del _bind_verified_completed_receipts_for_source


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

    def _validated_ack_receipt_impl(
        self,
        delta: CollectorDelta,
        *,
        _delta_validate,
        _receipt_type,
        _receipt_validate,
        _instant_parser,
    ) -> DesktopApplicationReceipt | None:
        """Return one fully verified durable ACK receipt or fail closed on corruption."""
        _delta_validate(delta)
        raw = self._read()
        acks = raw.get("acks")
        if type(acks) is not list:
            raise ApplicationReceiptError("desktop acknowledgement index is malformed")
        matches: list[dict[str, Any]] = []
        for item in acks:
            if type(item) is not dict:
                raise ApplicationReceiptError("desktop acknowledgement entry is malformed")
            if item.get("delta_id") == delta.delta_id:
                matches.append(item)
        if not matches:
            return None
        if len(matches) != 1:
            raise AckConflictError(
                f"multiple desktop acknowledgements exist for delta {delta.delta_id}"
            )
        item = matches[0]
        try:
            receipt = _receipt_type(
                delta_id=item["delta_id"],
                canonical_event_digest=item["canonical_event_digest"],
                receipt_id=item["application_receipt_id"],
                applied_at=item["applied_at"],
            )
            _receipt_validate(receipt)
            acknowledged = _instant_parser(
                item["acknowledged_at"],
                "acknowledged_at",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ApplicationReceiptError(
                "desktop acknowledgement entry is malformed"
            ) from exc
        if receipt.canonical_event_digest != delta.canonical_event_digest:
            raise AckConflictError(
                "existing desktop ack disagrees with collector evidence"
            )
        available = _instant_parser(
            delta.desktop_available_at,
            "desktop_available_at",
        )
        applied = _instant_parser(receipt.applied_at, "applied_at")
        if not (available <= applied <= acknowledged):
            raise ApplicationReceiptError(
                "existing desktop acknowledgement timing is invalid"
            )
        return receipt

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
        acks = raw.get("acks")
        if type(acks) is not list:
            raise ApplicationReceiptError("desktop acknowledgement index is malformed")
        existing_matches = []
        for item in acks:
            if type(item) is not dict:
                raise ApplicationReceiptError("desktop acknowledgement entry is malformed")
            if item.get("delta_id") == delta.delta_id:
                existing_matches.append(item)
        if len(existing_matches) > 1:
            raise AckConflictError(
                f"multiple desktop acknowledgements exist for delta {delta.delta_id}"
            )
        existing = existing_matches[0] if existing_matches else None
        if existing is not None:
            if existing.get("canonical_event_digest") != delta.canonical_event_digest:
                raise AckConflictError("existing desktop ack disagrees with applied event")
            if existing.get("application_receipt_id") != application_receipt.receipt_id:
                raise ApplicationReceiptError("existing desktop receipt disagrees with applied effect")
            if existing.get("applied_at") != application_receipt.applied_at:
                raise ApplicationReceiptError(
                    "existing desktop receipt timestamp disagrees with applied effect"
                )
            try:
                existing_applied = _instant(existing.get("applied_at"), "applied_at")
                existing_acknowledged = _instant(
                    existing.get("acknowledged_at"),
                    "acknowledged_at",
                )
            except (TypeError, ValueError) as exc:
                raise ApplicationReceiptError(
                    "existing desktop acknowledgement timing is malformed"
                ) from exc
            if not (desktop_available <= existing_applied <= existing_acknowledged):
                raise ApplicationReceiptError(
                    "existing desktop acknowledgement timing is invalid"
                )
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


def _bind_desktop_checkpoint_validated_ack(implementation):
    """Seal durable ACK validation roots outside mutable runtime dispatch."""

    delta_validate = CollectorDelta.validate
    receipt_type = DesktopApplicationReceipt
    receipt_validate = DesktopApplicationReceipt.validate
    instant_parser = _instant

    def validated_ack_receipt(
        self: DesktopDeltaCheckpointStore,
        delta: CollectorDelta,
    ) -> DesktopApplicationReceipt | None:
        return implementation(
            self,
            delta,
            _delta_validate=delta_validate,
            _receipt_type=receipt_type,
            _receipt_validate=receipt_validate,
            _instant_parser=instant_parser,
        )

    return validated_ack_receipt


DesktopDeltaCheckpointStore.validated_ack_receipt = (
    _bind_desktop_checkpoint_validated_ack(
        DesktopDeltaCheckpointStore._validated_ack_receipt_impl
    )
)
del DesktopDeltaCheckpointStore._validated_ack_receipt_impl
del _bind_desktop_checkpoint_validated_ack


class DesktopDeltaConsumer:
    """Apply one durable canonical event+health effect, then acknowledge the delta.

    ``apply_event`` is the complete idempotent application boundary and must not return a
    receipt until both canonical market persistence and required source-health persistence
    are durable. ``lookup_application_receipt`` recovers only such complete receipts.
    ``acknowledgement_clock`` separates operational ACK time from the causal ``as_of``
    visibility cutoff. ``on_application_receipt`` runs only after that durable receipt
    exists and a pre-delivery ACK-clock fence has been validated. When an operational
    clock exists, it is sampled again after delivery and that second sample is persisted
    as the ACK timestamp; rollback behind the pre-delivery sample leaves ACK absent. If
    delivery fails, the ACK remains absent and recovery replays the callback from the durable
    receipt without reapplying the canonical event/health transaction.
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
        acknowledgement_clock: Callable[[], str] | None = None,
        on_application_receipt: Callable[
            [CollectorDelta, DesktopApplicationReceipt], None
        ] | None = None,
        apply_health: Callable[[CollectorDelta, Any], None] | None = None,
    ) -> None:
        self.collector = collector
        self.checkpoint = checkpoint
        self.resolve_event = resolve_event
        self.apply_event = apply_event
        self.lookup_application_receipt = lookup_application_receipt
        if acknowledgement_clock is not None and not callable(acknowledgement_clock):
            raise TypeError("acknowledgement_clock must be callable or None")
        self._acknowledgement_clock = acknowledgement_clock
        if on_application_receipt is not None and not callable(on_application_receipt):
            raise TypeError("on_application_receipt must be callable or None")
        self._on_application_receipt = on_application_receipt
        if apply_health is not None:
            raise ApplicationReceiptError(
                "apply_health must be included inside the durable apply_event boundary"
            )

    def _acknowledged_at_impl(
        self,
        delta: CollectorDelta,
        receipt: DesktopApplicationReceipt,
        *,
        cutoff: datetime,
        clock: Callable[[], str] | None,
        not_before: datetime | None = None,
        _instant_parser,
    ) -> str:
        """Validate one exact operational ACK-clock sample for this handoff."""
        if clock is None:
            acknowledged = cutoff
        else:
            try:
                acknowledged = _instant_parser(
                    clock(),
                    "acknowledged_at",
                )
            except (TypeError, ValueError) as exc:
                raise ApplicationReceiptError(
                    "cannot establish canonical desktop acknowledgement time"
                ) from exc
            if acknowledged < cutoff:
                raise ApplicationReceiptError(
                    "desktop acknowledgement clock moved before the causal drain cutoff"
                )
            if not_before is not None and acknowledged < not_before:
                raise ApplicationReceiptError(
                    "desktop acknowledgement clock moved backward after receipt delivery"
                )

        available = _instant_parser(delta.desktop_available_at, "desktop_available_at")
        applied = _instant_parser(receipt.applied_at, "applied_at")
        if not (available <= applied <= acknowledged):
            raise ApplicationReceiptError(
                "application timing must satisfy desktop_available_at <= applied_at <= acknowledged_at"
            )
        return acknowledged.isoformat()

    def _drain_impl(
        self,
        *,
        as_of: str,
        view: CausalView = CausalView.AS_KNOWN_AT_DECISION,
        _receipt_type,
        _validate_receipt,
        _canonical_digest,
        _market_event_type,
        _acknowledged_at,
        _instant_parser,
        _validated_ack_receipt,
    ) -> tuple[str, ...]:
        now = _instant_parser(as_of, "as_of")
        available = self.collector.deltas_available_through(as_of=as_of, view=view)
        delivered: list[str] = []
        for delta in available:
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
                if _validated_ack_receipt(self.checkpoint, delta) is not None:
                    continue

                durable_receipt = self.lookup_application_receipt(delta)
                if durable_receipt is not None:
                    if type(durable_receipt) is not _receipt_type:
                        raise ApplicationReceiptError(
                            "durable application receipt must use the canonical receipt type"
                        )
                    _validate_receipt(durable_receipt)
                    if (
                        durable_receipt.delta_id != delta.delta_id
                        or durable_receipt.canonical_event_digest
                        != delta.canonical_event_digest
                    ):
                        raise ApplicationReceiptError(
                            f"durable application receipt is not bound to delta {delta.delta_id}"
                        )
                    acknowledgement_clock = self._acknowledgement_clock
                    on_application_receipt = self._on_application_receipt
                    acknowledged_at = _acknowledged_at(
                        self,
                        delta,
                        durable_receipt,
                        cutoff=now,
                        clock=acknowledgement_clock,
                    )
                    if on_application_receipt is not None:
                        on_application_receipt(delta, durable_receipt)
                        if acknowledgement_clock is not None:
                            acknowledged_at = _acknowledged_at(
                                self,
                                delta,
                                durable_receipt,
                                cutoff=now,
                                clock=acknowledgement_clock,
                                not_before=_instant_parser(
                                    acknowledged_at,
                                    "pre_delivery_acknowledged_at",
                                ),
                            )
                    self.checkpoint._ack_locked(
                        delta,
                        application_receipt=durable_receipt,
                        acknowledged_at=acknowledged_at,
                    )
                    delivered.append(delta.delta_id)
                    continue

                event = self.resolve_event(delta)
                if type(event) is not _market_event_type:
                    raise DeltaConflictError(
                        "desktop delta resolver must return an exact MarketEvent"
                    )
                digest = _canonical_digest(event)
                if digest != delta.canonical_event_digest:
                    raise DeltaConflictError(f"canonical event digest mismatch for delta {delta.delta_id}")
                receipt = self.apply_event(delta, event)
                if type(receipt) is not _receipt_type:
                    raise ApplicationReceiptError("apply_event must return a durable DesktopApplicationReceipt")
                _validate_receipt(receipt)
                if receipt.delta_id != delta.delta_id or receipt.canonical_event_digest != digest:
                    raise ApplicationReceiptError("application receipt is not bound to this delta/digest")
                acknowledgement_clock = self._acknowledgement_clock
                on_application_receipt = self._on_application_receipt
                acknowledged_at = _acknowledged_at(
                    self,
                    delta,
                    receipt,
                    cutoff=now,
                    clock=acknowledgement_clock,
                )
                if on_application_receipt is not None:
                    on_application_receipt(delta, receipt)
                    if acknowledgement_clock is not None:
                        acknowledged_at = _acknowledged_at(
                            self,
                            delta,
                            receipt,
                            cutoff=now,
                            clock=acknowledgement_clock,
                            not_before=_instant_parser(
                                acknowledged_at,
                                "pre_delivery_acknowledged_at",
                            ),
                        )
                self.checkpoint._ack_locked(
                    delta,
                    application_receipt=receipt,
                    acknowledged_at=acknowledged_at,
                )
                delivered.append(delta.delta_id)
        return tuple(delivered)


def _bind_desktop_delta_consumer_acknowledged_at(implementation):
    """Seal the ACK-time parser outside caller-writable function metadata."""

    instant_parser = _instant

    def acknowledged_at(
        self: DesktopDeltaConsumer,
        delta: CollectorDelta,
        receipt: DesktopApplicationReceipt,
        *,
        cutoff: datetime,
        clock: Callable[[], str] | None,
        not_before: datetime | None = None,
    ) -> str:
        return implementation(
            self,
            delta,
            receipt,
            cutoff=cutoff,
            clock=clock,
            not_before=not_before,
            _instant_parser=instant_parser,
        )

    return acknowledged_at


DesktopDeltaConsumer._acknowledged_at = (
    _bind_desktop_delta_consumer_acknowledged_at(
        DesktopDeltaConsumer._acknowledged_at_impl
    )
)
del DesktopDeltaConsumer._acknowledged_at_impl
del _bind_desktop_delta_consumer_acknowledged_at


def _bind_desktop_delta_consumer_drain(implementation):
    """Seal receipt, digest, ACK and default causal-view authority."""

    receipt_type = DesktopApplicationReceipt
    validate_receipt = DesktopApplicationReceipt.validate
    canonical_digest = canonical_event_digest
    market_event_type = MarketEvent
    acknowledged_at = DesktopDeltaConsumer._acknowledged_at
    instant_parser = _instant
    validated_ack_receipt = DesktopDeltaCheckpointStore.validated_ack_receipt
    default_view = CausalView.AS_KNOWN_AT_DECISION

    def drain(
        self,
        *,
        as_of: str,
        view: CausalView = CausalView.AS_KNOWN_AT_DECISION,
    ) -> tuple[str, ...]:
        if drain.__kwdefaults__ != {"view": default_view}:
            raise ApplicationReceiptError(
                "desktop consumer default causal-view metadata changed"
            )
        return implementation(
            self,
            as_of=as_of,
            view=view,
            _receipt_type=receipt_type,
            _validate_receipt=validate_receipt,
            _canonical_digest=canonical_digest,
            _market_event_type=market_event_type,
            _acknowledged_at=acknowledged_at,
            _instant_parser=instant_parser,
            _validated_ack_receipt=validated_ack_receipt,
        )

    return drain


DesktopDeltaConsumer.drain = _bind_desktop_delta_consumer_drain(
    DesktopDeltaConsumer._drain_impl
)
del DesktopDeltaConsumer._drain_impl
del _bind_desktop_delta_consumer_drain


class RemoteCollectorAdapter:
    DEPLOYMENT_STATUS = "NOT_DEPLOYED"
    REQUIREMENT = "REQUIRES_EXTERNAL_INFRASTRUCTURE"

    def __init__(self, commit_delta: Callable[[CollectorDelta], bool]) -> None:
        self._commit_delta = commit_delta

    def submit_committed_delta(self, delta: CollectorDelta) -> bool:
        delta.validate()
        return self._commit_delta(delta)
