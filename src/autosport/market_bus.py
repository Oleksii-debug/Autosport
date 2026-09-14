from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy

from .domain import MarketEvent
from .storage import SQLiteMarketStore


class MarketEventDeliveryError(ExceptionGroup):
    """Subscriber failures raised only after persistence, with the exact durable outcome."""

    def __new__(
        cls,
        message: str,
        exceptions: Sequence[Exception],
        accepted_events: Iterable[MarketEvent],
    ) -> "MarketEventDeliveryError":
        instance = super().__new__(cls, message, exceptions)
        instance.accepted_events = tuple(accepted_events)
        return instance

    def derive(self, exceptions: Sequence[Exception]) -> "MarketEventDeliveryError":
        return type(self)(self.message, exceptions, self.accepted_events)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_events)


class MarketEventBus:
    """Persist first, then attempt every subscriber once for each accepted event."""

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
        # Persistence has already succeeded. Keep an independent value snapshot for
        # delivery-error evidence and isolate every callback from mutable nested
        # metadata so one subscriber cannot rewrite another subscriber's view of
        # the durable accepted event.
        accepted_events = tuple(deepcopy(event) for event in events)
        subscribers = tuple(self.subscribers)
        failures: list[Exception] = []
        for event in accepted_events:
            for callback in subscribers:
                try:
                    callback(deepcopy(event))
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise MarketEventDeliveryError(
                "one or more market event subscribers failed after persistence",
                failures,
                accepted_events,
            )
