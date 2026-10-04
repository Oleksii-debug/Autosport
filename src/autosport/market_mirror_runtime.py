from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

from .domain import MarketEvent
from .market_mirror import MarketMirror, MirrorApplyResult, MirrorSnapshot, MirrorUpdate
from .market_state_identity import semantic_market_state_identity
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
        initial_keys = {
            (event.source_id, event.quote_key)
            for event in self._mirror.snapshot()
            if dependency.matches(event)
        }
        with self._lock:
            if normalized_id in self._dependencies:
                raise ValueError(f"input_id {normalized_id!r} is already registered")
            self._dependencies[normalized_id] = dependency
            self._matched_keys[normalized_id] = initial_keys
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

    def causal_view(self, input_id: str) -> MirrorSnapshot:
        """Read current product-issued state before freshness/status gating."""
        dependency = self._dependency(input_id)
        return self._mirror.causal_view(**self._selectors(dependency))

    def requires_current_history_fallback(
        self,
        input_id: str,
        *,
        as_of: datetime,
    ) -> bool:
        """Return whether latest-only state hides an earlier causally available value."""

        dependency = self._dependency(input_id)
        boundary, _ = MarketMirror._decision_boundary(
            as_of=as_of,
            max_age=timedelta(0),
        )
        current = self._mirror.causal_view(**self._selectors(dependency))
        return any(
            not MarketMirror._event_causally_available(
                event,
                boundary=boundary,
            )
            for event in current.events
        )

    def current_history_view(
        self,
        input_id: str,
        store: SQLiteMarketStore,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> MirrorSnapshot:
        """Resolve one live input from verified durable history without replay issuance."""

        dependency = self._dependency(input_id)
        return MarketMirror.current_history_view_from_store(
            store,
            as_of=as_of,
            max_age=max_age,
            **self._selectors(dependency),
        )

    def decision_state_from_proven_history(
        self,
        input_id: str,
        events_with_generation: tuple[tuple[MarketEvent, int], ...],
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> tuple[MirrorSnapshot, datetime | None]:
        """Resolve one input from a caller-owned snapshot already proven by storage."""

        dependency = self._dependency(input_id)
        boundary, age_limit = MarketMirror._decision_boundary(
            as_of=as_of,
            max_age=max_age,
        )
        snapshot = MarketMirror._decision_view_from_proven_history(
            events_with_generation,
            boundary=boundary,
            max_age=age_limit,
            **self._selectors(dependency),
        )
        state: dict[MirrorQuoteKey, tuple[MarketEvent, int]] = {}
        future_candidates: list[
            tuple[datetime, MirrorQuoteKey, MarketEvent, int]
        ] = []
        for event, append_generation in events_with_generation:
            if not dependency.matches(event):
                continue
            key = (event.source_id, event.quote_key)
            causal_times = MarketMirror._event_causal_times(event)
            if append_generation == 0:
                previous = state.get(key)
                if previous is None or event.sequence > previous[0].sequence:
                    state[key] = (event, append_generation)
                continue
            if append_generation < 0 or causal_times is None:
                continue
            available_at = max(causal_times)
            if available_at <= boundary:
                previous = state.get(key)
                if previous is None or event.sequence > previous[0].sequence:
                    state[key] = (event, append_generation)
            else:
                future_candidates.append(
                    (available_at, key, event, append_generation)
                )

        def visible_identity(
            item: tuple[MarketEvent, int] | None,
            *,
            at: datetime,
        ) -> str | None:
            if item is None or item[1] <= 0:
                return None
            event = item[0]
            if not MarketMirror._decision_visible_event(
                event,
                boundary=at,
                max_age=age_limit,
            ):
                return None
            semantic_identity = semantic_market_state_identity(event)
            return (
                semantic_identity
                if semantic_identity is not None
                else event.dedupe_key
            )

        ordered = sorted(
            future_candidates,
            key=lambda item: (item[0], item[1], item[2].sequence),
        )
        index = 0
        while index < len(ordered):
            available_at = ordered[index][0]
            before: dict[MirrorQuoteKey, str | None] = {}
            while (
                index < len(ordered)
                and ordered[index][0] == available_at
            ):
                _when, key, event, append_generation = ordered[index]
                if key not in before:
                    before[key] = visible_identity(
                        state.get(key),
                        at=available_at,
                    )
                previous = state.get(key)
                if previous is None or event.sequence > previous[0].sequence:
                    state[key] = (event, append_generation)
                index += 1

            if any(
                before[key]
                != visible_identity(state.get(key), at=available_at)
                for key in before
            ):
                return snapshot, available_at
        return snapshot, None

    def current_history_decision_state(
        self,
        input_id: str,
        store: SQLiteMarketStore,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> tuple[MirrorSnapshot, datetime | None]:
        """Resolve live state and its earliest future transition from verified history."""

        return self.decision_state_from_proven_history(
            input_id,
            tuple(store.events_with_append_generation()),
            as_of=as_of,
            max_age=max_age,
        )

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

    def _accept_with_causal_authority(
        self,
        event: MarketEvent,
        *,
        decision_causal: bool,
    ) -> MirrorApplyResult:
        """Atomically apply one durable event and publish any material invalidation."""

        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")
        if type(decision_causal) is not bool:
            raise TypeError("decision_causal must be a bool")

        with self._lock:
            result = self._mirror._apply_with_causal_authority(
                event,
                decision_causal=decision_causal,
            )
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

    def accept_persisted(self, event: MarketEvent) -> MirrorApplyResult:
        """Apply one newly product-issued durable event and publish its invalidation.

        MarketEventBus invokes this only for values returned by
        SQLiteMarketStore.append_batch_accepted(), so those values are positive
        product-issued append generations and are decision-causal by construction.
        """

        return self._accept_with_causal_authority(
            event,
            decision_causal=True,
        )

    def reconcile_persisted(
        self,
        event: MarketEvent,
        *,
        append_generation: int,
    ) -> MirrorApplyResult:
        """Reconcile proven history without losing provenance or invalidations.

        Generation zero is sealed migration/audit state rather than causal evidence.
        Positive generations are product-issued. Any APPLIED transition is still
        invalidated: an incomplete mirror can gain a causal value or lose one behind a
        higher non-causal sequence fence, and downstream decision inputs must refresh.
        """

        if type(append_generation) is not int or append_generation < 0:
            raise ValueError("append_generation must be a non-negative int")
        return self._accept_with_causal_authority(
            event,
            decision_causal=append_generation > 0,
        )

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
