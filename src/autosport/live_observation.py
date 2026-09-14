from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ingestion import IngestionEngine
from .ingestion_health import IngestionPolicy, SourceHealthStore
from .market_bus import MarketEventBus
from .providers import MarketProvider
from .session import ObservationResult
from .storage import SQLiteMarketStore


ObservationTask = Callable[[], ObservationResult]
Clock = Callable[[], str]


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
        except RuntimeError:
            # CPython reports OS/runtime inability to start a new thread as
            # RuntimeError. No observation task ran, so restore the single-flight
            # worker to idle and let the caller retry without restarting the app.
            self._thread = None
            with self._lock:
                self._busy = False
            return False
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


def observe_workspace_once(
    workspace: str | Path,
    provider: MarketProvider,
    *,
    max_items: int = 250,
    policy: IngestionPolicy | None = None,
    clock: Clock | None = None,
) -> ObservationResult:
    """Thread-safe workspace observation using short-lived store connections and no PaperBook."""

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteMarketStore(root / "market.db")
    health_store = SourceHealthStore(root / "source_health.json")
    try:
        engine = IngestionEngine(
            MarketEventBus(store),
            policy=policy,
            health_store=health_store,
            clock=clock,
        )
        stats = engine.poll_once(provider, max_items=max_items)
        current = tuple(
            sorted(
                (event for event in store.current().values() if event.source_id == provider.source_id),
                key=lambda event: (event.event_id, event.market_id, event.selection_id),
            )
        )
        return ObservationResult(stats, health_store.get(provider.source_id), current)
    finally:
        store.close()