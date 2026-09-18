from __future__ import annotations

import queue
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ingestion import CommittedIngestionHealthError, IngestionEngine, IngestionStats
from .ingestion_health import IngestionPolicy, SourceHealthStore
from .market_bus import MarketEventBus, MarketEventDeliveryError
from .market_mirror import MarketMirror
from .market_mirror_runtime import BoundedMirrorInvalidationBuffer
from .providers import MarketProvider, ProviderBatch
from .session import ObservationResult
from .storage import SQLiteMarketStore


ObservationTask = Callable[[], ObservationResult]
Clock = Callable[[], str]

_MAX_SNAPSHOT_BATCHES = 256
_MAX_BATCH_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class ObservationWorkerMessage:
    result: ObservationResult | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("worker message must contain exactly one of result or error")


class OneShotObservationWorker:
    """Runs acquisition away from Tk; caller consumes the terminal message on its own thread.

    The worker is deliberately non-daemon. A live observation mutates durable market/source-health
    state, so interpreter shutdown must not kill it in the middle of that persistence boundary.
    This is a durability invariant, not merely a thread-lifecycle implementation detail.
    """

    def __init__(self) -> None:
        self._messages: queue.Queue[ObservationWorkerMessage] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, task: ObservationTask) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        try:
            thread = threading.Thread(
                target=self._run,
                args=(task,),
                name="autosport-live-observation",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        except Exception as exc:
            # The live-observation caller reserves False for a genuinely busy
            # worker and schedules terminal polling whenever start() returns True.
            # Preserve that contract for any ordinary Thread construction/start
            # failure: publish one terminal error and let poll() restore idle state.
            self._thread = None
            self._messages.put(
                ObservationWorkerMessage(error=f"{type(exc).__name__}: {exc}")
            )
            return True
        return True

    def _run(self, task: ObservationTask) -> None:
        try:
            message = ObservationWorkerMessage(result=task())
        except BaseException as exc:
            # SystemExit/KeyboardInterrupt raised inside this background thread do
            # not terminate the GUI process. Publish a terminal failure so poll()
            # clears the single-flight state instead of leaving live observation
            # permanently busy after the worker thread has already died.
            message = ObservationWorkerMessage(error=f"{type(exc).__name__}: {exc}")
        self._messages.put(message)

    def poll(self) -> ObservationWorkerMessage | None:
        try:
            message = self._messages.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return message


class _ReplayableBatchProvider:
    """Hold one provider batch until the durable ingestion boundary acknowledges it."""

    def __init__(self, provider: MarketProvider) -> None:
        self._provider = provider
        self.source_id = provider.source_id
        self._inflight: ProviderBatch | None = None
        self._inflight_max_items: int | None = None
        self._carried_quality_flags: tuple[str, ...] = ()

    @property
    def has_inflight(self) -> bool:
        return self._inflight is not None

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if self._inflight is None:
            batch = self._provider.read_batch(max_items=max_items)
            if self._carried_quality_flags:
                batch = ProviderBatch(
                    source_id=batch.source_id,
                    quotes=batch.quotes,
                    cursor=batch.cursor,
                    quality_flags=tuple(
                        dict.fromkeys((*batch.quality_flags, *self._carried_quality_flags))
                    ),
                )
            self._inflight = batch
            self._inflight_max_items = max_items
        elif max_items != self._inflight_max_items:
            raise RuntimeError("cannot change live batch bound before durable acknowledgement")
        return self._inflight

    def carry_quality_flags(self, quality_flags: tuple[str, ...]) -> None:
        """Carry snapshot-wide degradation into later chunks, not pagination control state."""

        substantive = tuple(flag for flag in quality_flags if flag != "TRUNCATED_BATCH")
        if substantive:
            self._carried_quality_flags = tuple(
                dict.fromkeys((*self._carried_quality_flags, *substantive))
            )

    def acknowledge(self) -> None:
        if self._inflight is None:
            raise RuntimeError("cannot acknowledge a live batch that was not read")
        self._inflight = None
        self._inflight_max_items = None

    def abandon_uncommitted(self) -> None:
        """Prevent a reusable stateful provider from advancing past a never-durable batch."""

        if self._inflight is None:
            raise RuntimeError("cannot abandon a live batch that was not read")
        # The shipped Parlay provider owns an in-memory snapshot iterator. If both
        # bounded SQLite attempts fail before market commit, this wrapper is about
        # to unwind and lose its cached batch while that iterator already points at
        # the tail. Reset that snapshot so reusing the same provider refetches from
        # the beginning instead of silently skipping the never-durable prefix.
        reset_snapshot = getattr(self._provider, "reset_pending_snapshot", None)
        if not callable(reset_snapshot):
            reset_snapshot = getattr(self._provider, "_clear_pending_snapshot", None)
        if callable(reset_snapshot):
            reset_snapshot()
        self._inflight = None
        self._inflight_max_items = None
        self._carried_quality_flags = ()


def _poll_acknowledged(
    engine: IngestionEngine,
    provider: _ReplayableBatchProvider,
    *,
    max_items: int,
) -> IngestionStats:
    """Persist one batch before allowing the wrapped provider to advance again."""

    for attempt in range(_MAX_BATCH_ATTEMPTS):
        try:
            stats = engine.poll_once(provider, max_items=max_items)
        except CommittedIngestionHealthError:
            # The ingestion boundary guarantees market persistence already committed.
            # Retire this in-flight chunk before surfacing the health publication
            # failure; replaying it would corrupt accepted/progress accounting.
            if provider.has_inflight:
                provider.acknowledge()
            raise
        except MarketEventDeliveryError:
            # MarketEventBus raises this only after its storage transaction commits.
            # Do not replay already durable events merely because a subscriber failed.
            if provider.has_inflight:
                provider.acknowledge()
            raise
        except sqlite3.Error:
            # SQLiteMarketStore commits its batch transaction before poll_once can
            # proceed to source-health publication. A sqlite3.Error escaping that
            # market transaction is therefore the narrow failure class for which
            # replaying the still-inflight batch is safe and prevents tail loss.
            if not provider.has_inflight:
                raise
            if attempt + 1 >= _MAX_BATCH_ATTEMPTS:
                provider.abandon_uncommitted()
                raise
            continue
        except Exception:
            # Do not replay after an arbitrary later-stage failure (for example a
            # source-health JSON write): market events may already be durable, and
            # replaying them would turn a post-commit failure into misleading
            # accepted/progress accounting. Surface the failure instead.
            raise
        provider.acknowledge()
        return stats
    raise AssertionError("unreachable")


def _combined_stats(parts: list[IngestionStats]) -> IngestionStats:
    if not parts:
        raise ValueError("live snapshot stats require at least one persisted batch")
    last = parts[-1]
    return IngestionStats(
        source_id=last.source_id,
        received=sum(item.received for item in parts),
        accepted=sum(item.accepted for item in parts),
        rejected=sum(item.rejected for item in parts),
        elapsed_seconds=sum(item.elapsed_seconds for item in parts),
        cursor=last.cursor,
        # Intermediate TRUNCATED_BATCH is resolved by the drain, while substantive
        # snapshot-wide degradation is carried into the terminal batch before it is
        # persisted and therefore remains truthful in both stats and source health.
        quality_flags=last.quality_flags,
        health_status=last.health_status,
    )


def _drain_snapshot(
    engine: IngestionEngine,
    provider: MarketProvider,
    *,
    max_items: int,
) -> IngestionStats:
    replayable = _ReplayableBatchProvider(provider)
    parts: list[IngestionStats] = []
    for _batch_index in range(_MAX_SNAPSHOT_BATCHES):
        stats = _poll_acknowledged(engine, replayable, max_items=max_items)
        parts.append(stats)
        if "TRUNCATED_BATCH" not in stats.quality_flags:
            return _combined_stats(parts)
        replayable.carry_quality_flags(stats.quality_flags)
        if stats.received <= 0:
            raise RuntimeError(
                "provider reported TRUNCATED_BATCH without quote progress; "
                "live snapshot drain aborted"
            )
    raise RuntimeError(
        f"provider snapshot remained truncated after {_MAX_SNAPSHOT_BATCHES} bounded batches"
    )


def observe_workspace_once(
    workspace: str | Path,
    provider: MarketProvider,
    *,
    max_items: int = 250,
    policy: IngestionPolicy | None = None,
    clock: Clock | None = None,
    mirror_updates: BoundedMirrorInvalidationBuffer | None = None,
) -> ObservationResult:
    """Observe one bounded snapshot and feed the canonical persist-first Market Mirror.

    ``SQLiteMarketStore`` remains the sole durable market authority. A caller may pass
    a long-lived ``BoundedMirrorInvalidationBuffer`` to retain one provider-isolated
    in-memory decision view and bounded downstream invalidations across observations.
    If omitted, this call still composes the canonical mirror and returns current quotes
    from that mirror rather than maintaining a second ad-hoc live quote dictionary.
    """

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteMarketStore(root / "market.db")
    try:
        health_store = SourceHealthStore(root / "source_health.json")
        if mirror_updates is None:
            mirror = MarketMirror.from_store(store)
            mirror_updates = BoundedMirrorInvalidationBuffer(mirror)
        else:
            if not isinstance(mirror_updates, BoundedMirrorInvalidationBuffer):
                raise TypeError("mirror_updates must be a BoundedMirrorInvalidationBuffer")
            mirror = mirror_updates.mirror
            # Reconcile the non-durable mirror from canonical append-only history at
            # each observation boundary. Re-applying identical/stale events is
            # idempotent and deliberately does not enqueue downstream invalidations.
            for persisted_event in store.events():
                mirror.apply(persisted_event)

        bus = MarketEventBus(store)
        bus.subscribe(mirror_updates.accept_persisted)
        engine = IngestionEngine(
            bus,
            policy=policy,
            health_store=health_store,
            clock=clock,
        )
        stats = _drain_snapshot(engine, provider, max_items=max_items)
        source_id = stats.source_id
        current = tuple(
            sorted(
                mirror.view(source_ids=source_id).events,
                key=lambda event: (event.event_id, event.market_id, event.selection_id),
            )
        )
        return ObservationResult(stats, health_store.get(source_id), current)
    finally:
        store.close()
