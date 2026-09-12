from __future__ import annotations

from collections.abc import Callable

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
        if not self.store.append(event):
            return False
        for callback in tuple(self.subscribers):
            callback(event)
        return True

    def publish_many(self, events: list[MarketEvent]) -> int:
        accepted = 0
        for event in events:
            accepted += int(self.publish(event))
        return accepted
