from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ingestion import IngestionEngine, IngestionStats
from .ingestion_health import IngestionPolicy, SourceHealthStore
from .market_bus import MarketEventBus, MarketEventDeliveryError
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
        except RuntimeError as exc:
            # A request that won the single-flight slot must have exactly one
            # terminal poll outcome. Preserve that contract even when CPython/OS
            # cannot start the thread: publish a terminal error and let poll()
            # restore idle state. False remains reserved for a genuinely busy
            # worker, so GUI callers never misreport thread-start failure as
            # "live snapshot already running" and can retry after consuming it.
            self._thread = None
            self._messages.put(
                ObservationWorkerMessage(error=f"{type(exc).__name__}: {exc}")
            )
            return True
        return True

    def _run(self, task: ObservationTask) -> None:
        try:
            message = ObservationWorkerMessage(result=task())
        except Exception as exc:
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

    @property
    def has_inflight(self) -> bool:
        return self._inflight is not None

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if self._inflight is None:
            self._inflight = self._provider.read_batch(max_items=max_items)
            self._inflight_max_items = max_items
        elif max_items != self._inflight_max_items:
            raise RuntimeError("cannot change live batch bound before durable acknowledgement")
        return self._inflight

    def acknowledge(self) -> None:
        if self._inflight is None:
            raise RuntimeError("cannot acknowledge a live batch that was not read")
        self._inflight = None
        self._inflight_max_items = None


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
        except MarketEventDeliveryError:
            # MarketEventBus raises this only after its storage transaction commits.
            # Do not replay already durable events merely because a subscriber failed.
            if provider.has_inflight:
                provider.acknowledge()
            raise
        except Exception:
            # If acquisition returned a batch, keep that exact batch in-flight and
            # retry it rather than reading/advancing to a later provider chunk. A
            # deterministic validation failure simply fails again and propagates;
            # a transient local persistence failure can recover without quote loss.
            if not provider.has_inflight or attempt + 1 >= _MAX_BATCH_ATTEMPTS:
                raise
            continue
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
        # A completed snapshot must report the terminal quality truth. In
        # particular, intermediate TRUNCATED_BATCH flags are resolved by the drain.
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
) -> ObservationResult:
    """Thread-safe bounded snapshot observation using short-lived durable stores only."""

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteMarketStore(root / "market.db")
    try:
        health_store = SourceHealthStore(root / "source_health.json")
        engine = IngestionEngine(
            MarketEventBus(store),
            policy=policy,
            health_store=health_store,
            clock=clock,
        )
        stats = _drain_snapshot(engine, provider, max_items=max_items)
        current = tuple(
            sorted(
                (event for event in store.current().values() if event.source_id == provider.source_id),
                key=lambda event: (event.event_id, event.market_id, event.selection_id),
            )
        )
        return ObservationResult(stats, health_store.get(provider.source_id), current)
    finally:
        store.close()
