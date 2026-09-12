from __future__ import annotations

from collections.abc import Callable, Iterable

from .domain import MarketEvent
from .storage import SQLiteMarketStore


class MarketEventBus:
    """Single normalized ingestion boundary: persist first, then notify consumers exactly once."""

    def __init__(self, store: SQLiteMarketStore) -> None:
        self.store = store
        self.subscribers: list[Callable[[MarketEvent], None]] = []

    def subscribe(self, callback: Callable[[MarketEvent], None]) -> None:
        self.subscribers.append(callback)

    def publish(self, event: MarketEvent) -> bool:
        accepted = self.store.append_batch_accepted([event])
        if not accepted:
            return False
        self._notify(accepted)
        return True

    def publish_many(self, events: Iterable[MarketEvent]) -> int:
        accepted = self.store.append_batch_accepted(events)
        self._notify(accepted)
        return len(accepted)

    def _notify(self, events: Iterable[MarketEvent]) -> None:
        subscribers = tuple(self.subscribers)
        for event in events:
            for callback in subscribers:
                callback(event)
