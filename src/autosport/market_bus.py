from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy

from .domain import MarketEvent
from .storage import SQLiteMarketStore


def _snapshot_live_event(
    event: MarketEvent,
    *,
    _event_type: type[MarketEvent] = MarketEvent,
    _to_dict=MarketEvent.to_dict,
    _from_dict=MarketEvent.from_dict,
) -> MarketEvent:
    """Snapshot one live event through sealed canonical codec descriptors."""
    if type(event) is not _event_type:
        raise TypeError("live ingestion requires exact MarketEvent values")
    snapshot = _from_dict(_to_dict(event))
    if type(snapshot) is not _event_type:
        raise TypeError("live ingestion snapshot lost canonical MarketEvent authority")
    return snapshot


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


def _notify_live_subscribers(
    bus: object,
    events: Iterable[MarketEvent],
    *,
    _snapshot=_snapshot_live_event,
    _delivery_error_type=MarketEventDeliveryError,
) -> None:
    """Deliver only sealed snapshots of already receipt-authoritative live events."""
    accepted_events = tuple(_snapshot(event) for event in events)
    subscribers = tuple(bus.subscribers)
    failures: list[Exception] = []
    for event in accepted_events:
        for callback in subscribers:
            try:
                callback(_snapshot(event))
            except Exception as exc:
                failures.append(exc)
    if failures:
        raise _delivery_error_type(
            "one or more market event subscribers failed after persistence",
            failures,
            accepted_events,
        )


class MarketEventBus:
    """Persist first, then attempt every subscriber once for each accepted event."""

    def __init__(self, store: SQLiteMarketStore) -> None:
        self.store = store
        self.subscribers: list[Callable[[MarketEvent], None]] = []

    def subscribe(self, callback: Callable[[MarketEvent], None]) -> None:
        self.subscribers.append(callback)

    def publish(self, event: MarketEvent) -> bool:
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
        accepted = self.store.append_batch_accepted(deepcopy(event) for event in events)
        self._notify(accepted)
        return len(accepted)

    def _publish_many_live_ingestion(
        self,
        events: Iterable[MarketEvent],
        *,
        _store_type: type[SQLiteMarketStore] = SQLiteMarketStore,
        _live_append=SQLiteMarketStore._append_live_batch_accepted,
        _snapshot=_snapshot_live_event,
        _notify=_notify_live_subscribers,
    ) -> int:
        """Persist through the product-owned live-receipt authority seam.

        Generic bus publication intentionally remains provenance-neutral for replay,
        import and tests. IngestionEngine is the sole production caller of this private
        path after it overwrites ingest_ts from its post-acquisition product clock.
        """
        if type(self) is not __class__:
            raise TypeError("live ingestion requires an exact MarketEventBus")
        if type(self.store) is not _store_type:
            raise TypeError("live ingestion requires an exact SQLiteMarketStore")
        accepted = _live_append(
            self.store,
            (_snapshot(event) for event in events),
        )
        _notify(self, accepted)
        return len(accepted)

    def _notify(
        self,
        events: Iterable[MarketEvent],
        *,
        _deepcopy=deepcopy,
    ) -> None:
        # Persistence has already succeeded. Keep an independent value snapshot for
        # delivery-error evidence and isolate every callback from mutable nested
        # metadata so one subscriber cannot rewrite another subscriber's view of
        # the durable accepted event.
        accepted_events = tuple(_deepcopy(event) for event in events)
        subscribers = tuple(self.subscribers)
        failures: list[Exception] = []
        for event in accepted_events:
            for callback in subscribers:
                try:
                    callback(_deepcopy(event))
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise MarketEventDeliveryError(
                "one or more market event subscribers failed after persistence",
                failures,
                accepted_events,
            )

def _seal_live_bus_authority_call_surface() -> None:
    """Keep sealed live-publish dependencies internal to the canonical bus method."""

    live_publish_impl = MarketEventBus._publish_many_live_ingestion

    def _publish_many_live_ingestion(
        self: MarketEventBus,
        events: Iterable[MarketEvent],
    ) -> int:
        return live_publish_impl(self, events)

    MarketEventBus._publish_many_live_ingestion = _publish_many_live_ingestion


_seal_live_bus_authority_call_surface()
del _seal_live_bus_authority_call_surface
