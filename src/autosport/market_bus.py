from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy

from .domain import MarketEvent
from .storage import SQLiteMarketStore


def _subscriber_failure(exc: BaseException) -> Exception:
    """Normalize subscriber interrupts without losing post-commit disposition."""

    if isinstance(exc, Exception):
        return exc
    try:
        detail = str(exc)
    except BaseException:
        detail = "<unprintable exception>"
    wrapped = RuntimeError(
        f"subscriber raised {type(exc).__name__}: {detail}"
    )
    try:
        wrapped.add_note(
            "original subscriber failure was a BaseException and was normalized "
            "after market persistence committed"
        )
    except BaseException:
        pass
    return wrapped


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
        if type(store) is not SQLiteMarketStore:
            raise TypeError("store must be an exact SQLiteMarketStore")
        self.store = store
        self.subscribers: list[Callable[[MarketEvent], None]] = []

    def subscribe(self, callback: Callable[[MarketEvent], None]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self.subscribers.append(callback)

    def publish(self, event: MarketEvent) -> bool:
        if type(event) is not MarketEvent:
            raise TypeError("event must be an exact MarketEvent")
        # Snapshot at the bus boundary before persistence. MarketEvent is frozen,
        # but nested metadata is mutable; callers must not be able to rewrite the
        # value that later subscriber delivery/failure evidence says was durable.
        accepted = self.store.append_batch_accepted([deepcopy(event)])
        if not accepted:
            return False
        self._notify(accepted)
        return True

    def publish_many(self, events: Iterable[MarketEvent]) -> int:
        # Snapshot each item before yielding it to storage. This matters for lazy
        # iterables: after one item is persisted, advancing the producer may mutate
        # an earlier input object before append_batch_accepted() returns. Storage,
        # delivery, and accepted-event evidence must all stay bound to the same
        # ingress value.
        def snapshots() -> Iterable[MarketEvent]:
            for event in events:
                if type(event) is not MarketEvent:
                    raise TypeError("events must contain exact MarketEvent values")
                yield deepcopy(event)

        accepted = self.store.append_batch_accepted(snapshots())
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
                except BaseException as exc:
                    failures.append(_subscriber_failure(exc))
        if failures:
            raise MarketEventDeliveryError(
                "one or more market event subscribers failed after persistence",
                failures,
                accepted_events,
            )
