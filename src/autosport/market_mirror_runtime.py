from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

from .domain import MarketEvent
from .market_mirror import MarketMirror, MirrorApplyResult, MirrorSnapshot, MirrorUpdate
from .storage import SQLiteMarketStore


MirrorQuoteKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class MirrorInvalidationBatch:
    """Bounded downstream work derived from already-applied durable market updates.

    ``full_refresh_required`` is an explicit fail-safe fence. When it is true,
    ``changed_keys`` is intentionally empty: bounded key tracking saturated, so a
    consumer must refresh from one coherent ``MarketMirror.view()`` rather than
    pretending that an incomplete affected-key list is authoritative.
    """

    changed_keys: tuple[MirrorQuoteKey, ...]
    full_refresh_required: bool
    has_more: bool


@dataclass(frozen=True, slots=True)
class FocusedMirrorDependency:
    """Immutable decision-input selectors over canonical Market Mirror truth."""

    input_id: str
    source_ids: frozenset[str] | None
    sports: frozenset[str] | None
    event_ids: frozenset[str] | None
    market_ids: frozenset[str] | None
    selection_ids: frozenset[str] | None

    def matches(self, event: MarketEvent) -> bool:
        return (
            (self.source_ids is None or event.source_id in self.source_ids)
            and (self.sports is None or event.sport in self.sports)
            and (self.event_ids is None or event.event_id in self.event_ids)
            and (self.market_ids is None or event.market_id in self.market_ids)
            and (
                self.selection_ids is None
                or event.selection_id in self.selection_ids
            )
        )


class FocusedMirrorDependencyIndex:
    """Route canonical quote invalidations only to affected decision inputs.

    The index stores selector metadata, never quote values. Current decision state is
    always read from the shared ``MarketMirror`` and historical decision state is
    reconstructed from ``SQLiteMarketStore`` through the mirror's causal replay path.
    That keeps one market-state authority while letting opportunities, portfolio
    calculations, critics, or other decision inputs subscribe to narrow dependencies.

    A bounded invalidation overflow intentionally invalidates every registered input:
    the changed-key list is incomplete in that state, so a conservative full refresh
    is the only truthful result.
    """

    def __init__(self, mirror: MarketMirror) -> None:
        if not isinstance(mirror, MarketMirror):
            raise TypeError("mirror must be a MarketMirror")
        self._mirror = mirror
        self._dependencies: dict[str, FocusedMirrorDependency] = {}
        self._matched_keys: dict[str, set[MirrorQuoteKey]] = {}
        self._lock = RLock()

    @staticmethod
    def _input_id(value: str) -> str:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError("input_id must be a non-empty trimmed string")
        return value

    @staticmethod
    def _selector(
        values: str | Iterable[str] | None,
        *,
        name: str,
    ) -> frozenset[str] | None:
        # Reuse the canonical focused-view normalization contract so registration and
        # later mirror reads cannot disagree about selector semantics.
        return MarketMirror._selector(values, name=name)

    def register(
        self,
        input_id: str,
        *,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
    ) -> FocusedMirrorDependency:
        """Register one immutable focused dependency without copying market state."""
        normalized_id = self._input_id(input_id)
        dependency = FocusedMirrorDependency(
            input_id=normalized_id,
            source_ids=self._selector(source_ids, name="source_ids"),
            sports=self._selector(sports, name="sports"),
            event_ids=self._selector(event_ids, name="event_ids"),
            market_ids=self._selector(market_ids, name="market_ids"),
            selection_ids=self._selector(selection_ids, name="selection_ids"),
        )
        with self._lock:
            if normalized_id in self._dependencies:
                raise ValueError(f"input_id {normalized_id!r} is already registered")

            # Publish the dependency before capturing its initial mirror keys while
            # holding the index lock. This makes registration linearizable with
            # affected_inputs(): an update routed after publication either lands in
            # this provisional key set or waits until the initial snapshot is merged.
            # Without this ordering, an update can be applied and its invalidation
            # fully routed between snapshot() and dependency publication, leaving the
            # incremental view permanently unaware of a current matching quote.
            self._dependencies[normalized_id] = dependency
            self._matched_keys[normalized_id] = set()
            try:
                initial_keys = {
                    (event.source_id, event.quote_key)
                    for event in self._mirror.snapshot()
                    if dependency.matches(event)
                }
            except BaseException:
                self._dependencies.pop(normalized_id, None)
                self._matched_keys.pop(normalized_id, None)
                raise
            self._matched_keys[normalized_id].update(initial_keys)
        return dependency

    def unregister(self, input_id: str) -> bool:
        normalized_id = self._input_id(input_id)
        with self._lock:
            removed = self._dependencies.pop(normalized_id, None)
            self._matched_keys.pop(normalized_id, None)
            return removed is not None

    @property
    def input_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._dependencies)

    def _dependency(self, input_id: str) -> FocusedMirrorDependency:
        normalized_id = self._input_id(input_id)
        with self._lock:
            try:
                return self._dependencies[normalized_id]
            except KeyError as exc:
                raise KeyError(f"unknown focused mirror input {normalized_id!r}") from exc

    def affected_inputs(self, batch: MirrorInvalidationBatch) -> tuple[str, ...]:
        """Return registered decision inputs affected by one drained invalidation batch."""
        if not isinstance(batch, MirrorInvalidationBatch):
            raise TypeError("batch must be a MirrorInvalidationBatch")

        with self._lock:
            dependencies = tuple(self._dependencies.values())

        if batch.full_refresh_required:
            snapshot = self._mirror.snapshot()
            rebuilt = {
                dependency.input_id: {
                    (event.source_id, event.quote_key)
                    for event in snapshot
                    if dependency.matches(event)
                }
                for dependency in dependencies
            }
            with self._lock:
                for dependency in dependencies:
                    if self._dependencies.get(dependency.input_id) == dependency:
                        self._matched_keys[dependency.input_id] = rebuilt[
                            dependency.input_id
                        ]
            return tuple(dependency.input_id for dependency in dependencies)
        if not batch.changed_keys or not dependencies:
            return ()

        changed_events = tuple(
            event
            for source_id, quote_key in batch.changed_keys
            if (
                event := self._mirror.event_for_quote_key(source_id, quote_key)
            ) is not None
        )
        if not changed_events:
            return ()

        affected: list[str] = []
        with self._lock:
            for dependency in dependencies:
                if self._dependencies.get(dependency.input_id) != dependency:
                    continue
                matched = self._matched_keys.setdefault(dependency.input_id, set())
                dependency_affected = False
                for event in changed_events:
                    if not dependency.matches(event):
                        continue
                    matched.add((event.source_id, event.quote_key))
                    dependency_affected = True
                if dependency_affected:
                    affected.append(dependency.input_id)
        return tuple(affected)

    @staticmethod
    def _selectors(dependency: FocusedMirrorDependency) -> dict[str, frozenset[str] | None]:
        return {
            "source_ids": dependency.source_ids,
            "sports": dependency.sports,
            "event_ids": dependency.event_ids,
            "market_ids": dependency.market_ids,
            "selection_ids": dependency.selection_ids,
        }

    def matching_keys(self, input_id: str) -> tuple[MirrorQuoteKey, ...]:
        """Return immutable quote identities known to match one registered input."""
        normalized_id = self._input_id(input_id)
        with self._lock:
            if normalized_id not in self._dependencies:
                raise KeyError(f"unknown focused mirror input {normalized_id!r}")
            return tuple(sorted(self._matched_keys.get(normalized_id, set())))

    def all_matching_keys(self) -> tuple[MirrorQuoteKey, ...]:
        """Return the union of registered dependency identities, never quote values."""
        with self._lock:
            keys: set[MirrorQuoteKey] = set()
            for input_id in self._dependencies:
                keys.update(self._matched_keys.get(input_id, set()))
        return tuple(sorted(keys))

    def decision_view(
        self,
        input_id: str,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> MirrorSnapshot:
        """Read one canonical focused view without depending on invalidation drains."""
        dependency = self._dependency(input_id)
        return self._mirror.active_view(
            as_of=as_of,
            max_age=max_age,
            **self._selectors(dependency),
        )

    def incremental_decision_view(
        self,
        input_id: str,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> MirrorSnapshot:
        """Read one focused view through quote keys maintained by invalidation routing.

        This bounded projection is valid for consumers that first route every drained
        invalidation batch through affected_inputs, or use the explicit full-refresh
        path. General consumers must use decision_view so correctness does not depend
        on participating in this index invalidation protocol.
        """
        keys = self.matching_keys(input_id)
        return self._mirror.active_view_for_keys(
            keys,
            as_of=as_of,
            max_age=max_age,
        )

    def replay_view(
        self,
        input_id: str,
        store: SQLiteMarketStore,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> MirrorSnapshot:
        """Reconstruct the same focused decision input at an historical timestamp."""
        dependency = self._dependency(input_id)
        return MarketMirror.replay_view_from_store(
            store,
            as_of=as_of,
            max_age=max_age,
            **self._selectors(dependency),
        )


class BoundedMirrorInvalidationBuffer:
    """Persist-first Market Mirror subscriber with bounded downstream invalidations.

    Register :meth:`accept_persisted` as a ``MarketEventBus`` subscriber. The bus
    persists first, then calls subscribers, so this bridge never becomes storage
    authority. Every accepted event is applied synchronously to ``MarketMirror``;
    only the downstream invalidation keys are buffered.

    Repeated material updates to one quote coalesce to one dirty key. If more
    distinct keys become dirty than ``max_dirty_keys`` can represent, the buffer
    drops the incomplete key list and raises an explicit full-refresh fence. The
    mirror itself is still current, so no durable market update is lost merely to
    keep downstream recomputation bounded.
    """

    def __init__(
        self,
        mirror: MarketMirror,
        *,
        max_dirty_keys: int = 4096,
    ) -> None:
        if not isinstance(mirror, MarketMirror):
            raise TypeError("mirror must be a MarketMirror")
        if (
            isinstance(max_dirty_keys, bool)
            or not isinstance(max_dirty_keys, int)
            or max_dirty_keys <= 0
        ):
            raise ValueError("max_dirty_keys must be a positive non-boolean integer")
        self._mirror = mirror
        self._max_dirty_keys = max_dirty_keys
        self._dirty: dict[MirrorQuoteKey, None] = {}
        self._full_refresh_required = False
        self._lock = RLock()

    @property
    def mirror(self) -> MarketMirror:
        return self._mirror

    @property
    def max_dirty_keys(self) -> int:
        return self._max_dirty_keys

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._dirty)

    @property
    def full_refresh_required(self) -> bool:
        with self._lock:
            return self._full_refresh_required

    def accept_persisted(self, event: MarketEvent) -> MirrorApplyResult:
        """Apply one already-durable event and record its affected quote if material.

        The tracker lock covers both mirror mutation and invalidation publication so
        a concurrent ``drain`` cannot observe an applied mirror update before its
        downstream invalidation state has been established.
        """
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")

        with self._lock:
            result = self._mirror.apply(event)
            if result.status is not MirrorUpdate.APPLIED:
                return result

            if self._full_refresh_required:
                return result

            key = (result.source_id, result.quote_key)
            if key in self._dirty:
                return result

            if len(self._dirty) >= self._max_dirty_keys:
                # Never publish a partial affected-key list as complete truth.
                # The mirror already contains this update, so degrade to one
                # coherent full refresh rather than dropping durable state.
                self._dirty.clear()
                self._full_refresh_required = True
                return result

            self._dirty[key] = None
            return result

    def drain(self, *, max_items: int = 250) -> MirrorInvalidationBatch:
        """Return at most ``max_items`` affected keys, or one full-refresh fence."""
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")

        with self._lock:
            if self._full_refresh_required:
                self._full_refresh_required = False
                self._dirty.clear()
                return MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=True,
                    has_more=False,
                )

            count = min(max_items, len(self._dirty))
            keys = tuple(list(self._dirty)[:count])
            for key in keys:
                del self._dirty[key]
            return MirrorInvalidationBatch(
                changed_keys=keys,
                full_refresh_required=False,
                has_more=bool(self._dirty),
            )
