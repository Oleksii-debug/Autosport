from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from threading import RLock

from .domain import MarketEvent
from .market_mirror import MarketMirror, MirrorApplyResult
from .storage import SQLiteMarketStore


_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


class _DurableStoreFailure(RuntimeError):
    """A durable append failed without a proven terminal event conflict."""


class BufferSubmit(str, Enum):
    ACCEPTED = "accepted"
    BACKPRESSURE = "backpressure"


@dataclass(frozen=True, slots=True)
class BufferSubmitResult:
    status: BufferSubmit
    source_id: str
    queued: int
    capacity: int


@dataclass(frozen=True, slots=True)
class QuarantinedUpdate:
    source_id: str
    event: MarketEvent
    error_type: str
    error_message: str


@dataclass(frozen=True, slots=True)
class ProviderDrainResult:
    source_id: str
    applied: tuple[MirrorApplyResult, ...]
    quarantined: tuple[QuarantinedUpdate, ...]
    remaining: int


class MarketMirrorUpdateBuffer:
    """Bounded provider-isolated staging for incremental MarketMirror updates.

    The buffer is intentionally non-durable and owns no market truth. Provider adapters
    submit normalized ``MarketEvent`` values; a consumer drains one provider FIFO into
    the canonical ``MarketMirror``. Capacity is enforced independently per provider so
    a burst or malformed update from one source cannot consume another source's queue.

    BACKPRESSURE is explicit and never drops an accepted queued update. Validation
    conflicts are quarantined for the affected provider and draining continues. Other
    apply/persistence failures retain the queue head and propagate so accepted evidence
    cannot disappear before durable acknowledgement.

    When ``store`` is supplied, draining appends to the canonical ``SQLiteMarketStore``
    before live mirror mutation. A store rejection is terminal only when authoritative
    history proves the incoming source-local identity conflicts with an existing event;
    otherwise the queue head is retained for retry/repair. With no store, the buffer
    remains an explicitly in-memory composition surface.
    """

    def __init__(
        self,
        mirror: MarketMirror,
        *,
        store: SQLiteMarketStore | None = None,
        per_provider_capacity: int = 256,
    ) -> None:
        if not isinstance(mirror, MarketMirror):
            raise TypeError("mirror must be a MarketMirror")
        if store is not None and not isinstance(store, SQLiteMarketStore):
            raise TypeError("store must be a SQLiteMarketStore or None")
        if not isinstance(per_provider_capacity, int) or isinstance(per_provider_capacity, bool):
            raise TypeError("per_provider_capacity must be an integer")
        if per_provider_capacity < 1:
            raise ValueError("per_provider_capacity must be at least 1")
        self._mirror = mirror
        self._store = store
        self._capacity = per_provider_capacity
        self._queues: dict[str, deque[MarketEvent]] = {}
        self._provider_drain_locks: dict[str, RLock] = {}
        self._lock = RLock()

    @staticmethod
    def _snapshot(event: MarketEvent) -> MarketEvent:
        return MarketEvent.from_dict(event.to_dict())

    @staticmethod
    def _source_payload(event: MarketEvent) -> dict[str, object]:
        payload = event.to_dict()
        payload.pop("observed_ts", None)
        payload.pop("ingest_ts", None)
        return payload

    @staticmethod
    def _validate_durable_event(event: MarketEvent) -> None:
        """Fail terminal malformed input before entering the durable append boundary."""
        if isinstance(event.sequence, bool) or not isinstance(event.sequence, int):
            raise ValueError("market event sequence must be a non-boolean int")
        if event.sequence < _SQLITE_INTEGER_MIN or event.sequence > _SQLITE_INTEGER_MAX:
            raise ValueError("market event sequence must fit signed 64-bit SQLite INTEGER")
        for field_name, value in (
            ("observed_ts", event.observed_ts),
            ("ingest_ts", event.ingest_ts),
        ):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (AttributeError, ValueError) as exc:
                raise ValueError(f"{field_name} must be valid ISO-8601") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware ISO-8601")

    def _durable_duplicate_conflict(self, event: MarketEvent) -> bool:
        """Use authoritative history to distinguish event conflict from store damage."""
        if self._store is None:
            return False
        for existing in self._store.events(event.event_id):
            if existing.dedupe_key != event.dedupe_key:
                continue
            return self._source_payload(existing) != self._source_payload(event)
        return False

    def _provider_drain_lock(self, source_id: str) -> RLock:
        with self._lock:
            return self._provider_drain_locks.setdefault(source_id, RLock())

    def _apply(self, event: MarketEvent) -> MirrorApplyResult:
        if self._store is None:
            return self._mirror.apply(event)

        self._validate_durable_event(event)
        try:
            self._store.append(event)
        except (TypeError, ValueError) as exc:
            # Invalid staged input was already rejected above. A remaining store-side
            # TypeError/ValueError is safe to consume only when canonical history proves
            # this exact source-local identity is a conflicting duplicate. If history
            # itself cannot be decoded, that is also a durable integrity failure.
            try:
                terminal_conflict = self._durable_duplicate_conflict(event)
            except (TypeError, ValueError) as integrity_exc:
                raise _DurableStoreFailure(
                    "durable market history failed integrity validation"
                ) from integrity_exc
            if terminal_conflict:
                raise
            raise _DurableStoreFailure(
                "durable market append failed before acknowledgement"
            ) from exc
        return self._mirror.apply(event)

    def submit(self, event: MarketEvent) -> BufferSubmitResult:
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")
        with self._lock:
            queue = self._queues.setdefault(event.source_id, deque())
            if len(queue) >= self._capacity:
                return BufferSubmitResult(
                    BufferSubmit.BACKPRESSURE,
                    event.source_id,
                    len(queue),
                    self._capacity,
                )
            queue.append(self._snapshot(event))
            return BufferSubmitResult(
                BufferSubmit.ACCEPTED,
                event.source_id,
                len(queue),
                self._capacity,
            )

    def queued(self, source_id: str | None = None) -> int:
        with self._lock:
            if source_id is None:
                return sum(len(queue) for queue in self._queues.values())
            return len(self._queues.get(source_id, ()))

    def drain_provider(self, source_id: str, *, limit: int | None = None) -> ProviderDrainResult:
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source_id must be a non-empty string")
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise TypeError("limit must be an integer or None")
            if limit < 1:
                raise ValueError("limit must be at least 1")

        applied: list[MirrorApplyResult] = []
        quarantined: list[QuarantinedUpdate] = []
        processed = 0

        # One provider has one drain critical section. Different providers can still
        # progress independently because the shared queue lock is held only for short
        # queue operations, never across mirror/store I/O.
        with self._provider_drain_lock(source_id):
            while limit is None or processed < limit:
                with self._lock:
                    queue = self._queues.get(source_id)
                    if not queue:
                        break
                    # Peek first. The accepted update is committed out of staging only
                    # after the canonical apply/persist-first outcome is known.
                    event = queue[0]

                try:
                    result = self._apply(event)
                except (TypeError, ValueError) as exc:
                    quarantined.append(
                        QuarantinedUpdate(
                            source_id=source_id,
                            event=self._snapshot(event),
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
                else:
                    applied.append(result)

                # Validation conflicts and successful applications are terminal for this
                # staged item. Operational/persistence exceptions are intentionally not
                # caught above, so they propagate with the queue head still present.
                with self._lock:
                    queue = self._queues.get(source_id)
                    if not queue:
                        raise RuntimeError("provider queue changed during serialized drain")
                    queue.popleft()
                processed += 1

            with self._lock:
                remaining = len(self._queues.get(source_id, ()))
                if remaining == 0:
                    self._queues.pop(source_id, None)

        return ProviderDrainResult(
            source_id=source_id,
            applied=tuple(applied),
            quarantined=tuple(quarantined),
            remaining=remaining,
        )
