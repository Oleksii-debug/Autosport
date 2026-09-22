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
    mirror_revision: int | None = None


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

    def __init__(
        self,
        mirror: MarketMirror,
        *,
        max_cached_keys_per_input: int = 4096,
        max_cached_keys_total: int = 4096,
    ) -> None:
        if not isinstance(mirror, MarketMirror):
            raise TypeError("mirror must be a MarketMirror")
        for name, value in (
            ("max_cached_keys_per_input", max_cached_keys_per_input),
            ("max_cached_keys_total", max_cached_keys_total),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(
                    f"{name} must be a positive non-boolean integer"
                )
        self._mirror = mirror
        self._max_cached_keys_per_input = max_cached_keys_per_input
        self._max_cached_keys_total = max_cached_keys_total
        self._cached_key_count = 0
        self._dependencies: dict[str, FocusedMirrorDependency] = {}
        self._matched_keys: dict[str, set[MirrorQuoteKey]] = {}
        self._matched_revisions: dict[str, int] = {}
        self._incomplete_keysets: set[str] = set()
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

    @property
    def max_cached_keys_per_input(self) -> int:
        """Maximum exact routed quote identities retained for one dependency."""
        return self._max_cached_keys_per_input

    @property
    def max_cached_keys_total(self) -> int:
        """Maximum exact routed quote identities retained across the whole index."""
        return self._max_cached_keys_total

    @property
    def cached_key_count(self) -> int:
        """Current logical resident key count across all complete derived caches."""
        with self._lock:
            return self._cached_key_count

    def routed_cache_complete(self, input_id: str) -> bool:
        """Report whether one dependency's bounded routed-key cache is complete."""
        normalized_id = self._input_id(input_id)
        with self._lock:
            if normalized_id not in self._dependencies:
                raise KeyError(f"unknown focused mirror input {normalized_id!r}")
            return normalized_id not in self._incomplete_keysets

    def _bounded_matching_keys(
        self,
        events: Iterable[MarketEvent],
        dependency: FocusedMirrorDependency,
    ) -> tuple[set[MirrorQuoteKey], bool]:
        """Build an exact bounded derived keyset or explicitly mark it incomplete.

        A partial keyset must never be consumed as complete decision truth. Once the
        budget would be exceeded, discard the derived keys and make the caller use a
        coherent selector read from the canonical MarketMirror instead.
        """
        keys: set[MirrorQuoteKey] = set()
        for event in events:
            if not dependency.matches(event):
                continue
            key = (event.source_id, event.quote_key)
            if key in keys:
                continue
            if len(keys) >= self._max_cached_keys_per_input:
                return set(), False
            keys.add(key)
        return keys, True

    def _replace_cached_keys(
        self,
        input_id: str,
        keys: set[MirrorQuoteKey],
        *,
        complete: bool,
        revision: int,
    ) -> bool:
        """Replace one cache under lock without exceeding the global key budget."""
        previous = self._matched_keys.get(input_id, set())
        resident_without_previous = self._cached_key_count - len(previous)
        candidate = keys if complete else set()
        if (
            complete
            and resident_without_previous + len(candidate)
            > self._max_cached_keys_total
        ):
            complete = False
            candidate = set()

        self._matched_keys[input_id] = candidate
        self._cached_key_count = resident_without_previous + len(candidate)
        self._matched_revisions[input_id] = revision
        if complete:
            self._incomplete_keysets.discard(input_id)
        else:
            self._incomplete_keysets.add(input_id)
        return complete

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
        captured = self._mirror.view()
        initial_keys, initial_complete = self._bounded_matching_keys(
            captured.events,
            dependency,
        )
        with self._lock:
            if normalized_id in self._dependencies:
                raise ValueError(f"input_id {normalized_id!r} is already registered")
            self._dependencies[normalized_id] = dependency
            self._replace_cached_keys(
                normalized_id,
                initial_keys,
                complete=initial_complete,
                revision=captured.revision,
            )

            # Close the capture -> publication race without making a second mirror
            # authority. If the canonical mirror advanced before registration became
            # visible, refresh from one later coherent revision while the dependency
            # publication lock is still held. Updates after this point are ordinary
            # post-registration invalidations and route through affected_inputs().
            published = self._mirror.view()
            if published.revision != captured.revision:
                published_keys, published_complete = self._bounded_matching_keys(
                    published.events,
                    dependency,
                )
                self._replace_cached_keys(
                    normalized_id,
                    published_keys,
                    complete=published_complete,
                    revision=published.revision,
                )
        return dependency

    def unregister(self, input_id: str) -> bool:
        normalized_id = self._input_id(input_id)
        with self._lock:
            removed = self._dependencies.pop(normalized_id, None)
            removed_keys = self._matched_keys.pop(normalized_id, set())
            self._cached_key_count -= len(removed_keys)
            self._matched_revisions.pop(normalized_id, None)
            self._incomplete_keysets.discard(normalized_id)
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
        """Route one drained invalidation batch and advance keyset completeness."""
        if not isinstance(batch, MirrorInvalidationBatch):
            raise TypeError("batch must be a MirrorInvalidationBatch")

        with self._lock:
            dependencies = tuple(self._dependencies.values())

        if batch.full_refresh_required:
            captured = self._mirror.view()
            with self._lock:
                for dependency in dependencies:
                    if self._dependencies.get(dependency.input_id) != dependency:
                        continue
                    keys, complete = self._bounded_matching_keys(
                        captured.events,
                        dependency,
                    )
                    self._replace_cached_keys(
                        dependency.input_id,
                        keys,
                        complete=complete,
                        revision=captured.revision,
                    )
            return tuple(dependency.input_id for dependency in dependencies)

        if not dependencies:
            return ()

        if not batch.changed_keys:
            if not batch.has_more and batch.mirror_revision is not None:
                with self._lock:
                    for dependency in dependencies:
                        if self._dependencies.get(dependency.input_id) != dependency:
                            continue
                        previous = self._matched_revisions.get(dependency.input_id)
                        if previous is None or batch.mirror_revision > previous:
                            self._matched_revisions[dependency.input_id] = (
                                batch.mirror_revision
                            )
            return ()

        changed_events = tuple(
            event
            for source_id, quote_key in batch.changed_keys
            if (
                event := self._mirror.event_for_quote_key(source_id, quote_key)
            ) is not None
        )
        affected: list[str] = []
        with self._lock:
            for dependency in dependencies:
                if self._dependencies.get(dependency.input_id) != dependency:
                    continue
                matched = self._matched_keys.setdefault(dependency.input_id, set())
                cache_complete = (
                    dependency.input_id not in self._incomplete_keysets
                )
                dependency_affected = False
                for event in changed_events:
                    if not dependency.matches(event):
                        continue
                    dependency_affected = True
                    if not cache_complete:
                        continue
                    key = (event.source_id, event.quote_key)
                    if key in matched:
                        continue
                    if (
                        len(matched) >= self._max_cached_keys_per_input
                        or self._cached_key_count >= self._max_cached_keys_total
                    ):
                        # Do not evict one identity and silently lose selector truth.
                        # Saturation converts this derived cache to explicit fallback
                        # mode; the canonical MarketMirror remains intact.
                        self._cached_key_count -= len(matched)
                        matched.clear()
                        self._incomplete_keysets.add(dependency.input_id)
                        cache_complete = False
                        continue
                    matched.add(key)
                    self._cached_key_count += 1
                if dependency_affected:
                    affected.append(dependency.input_id)

            # A terminal drain is a causal completeness checkpoint for the canonical
            # buffer revision represented by this batch. Never advance beyond that
            # revision: a newer mirror update may already exist but still be pending
            # in a later invalidation batch.
            if not batch.has_more and batch.mirror_revision is not None:
                for dependency in dependencies:
                    if self._dependencies.get(dependency.input_id) != dependency:
                        continue
                    previous = self._matched_revisions.get(dependency.input_id)
                    if previous is None or batch.mirror_revision > previous:
                        self._matched_revisions[dependency.input_id] = (
                            batch.mirror_revision
                        )
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
        """Return exact cached identities only when the bounded cache is complete."""
        normalized_id = self._input_id(input_id)
        with self._lock:
            if normalized_id not in self._dependencies:
                raise KeyError(f"unknown focused mirror input {normalized_id!r}")
            if normalized_id in self._incomplete_keysets:
                raise RuntimeError(
                    "focused mirror routed key cache is incomplete; "
                    "use a canonical decision view"
                )
            return tuple(sorted(self._matched_keys.get(normalized_id, set())))

    def all_matching_keys(self) -> tuple[MirrorQuoteKey, ...]:
        """Return the exact cache union, never an incomplete derived subset."""
        with self._lock:
            if any(
                input_id in self._incomplete_keysets
                for input_id in self._dependencies
            ):
                raise RuntimeError(
                    "focused mirror routed key cache is incomplete; "
                    "use canonical decision views"
                )
            keys: set[MirrorQuoteKey] = set()
            for input_id in self._dependencies:
                keys.update(self._matched_keys.get(input_id, set()))
        return tuple(sorted(keys))

    def _batch_dependencies(
        self,
        input_ids: tuple[str, ...],
    ) -> tuple[FocusedMirrorDependency, ...]:
        if type(input_ids) is not tuple:
            raise TypeError("input_ids must be a tuple")
        normalized = tuple(self._input_id(input_id) for input_id in input_ids)
        if len(normalized) != len(set(normalized)):
            raise ValueError("input_ids must be unique")

        dependencies: list[FocusedMirrorDependency] = []
        with self._lock:
            for input_id in normalized:
                try:
                    dependencies.append(self._dependencies[input_id])
                except KeyError as exc:
                    raise KeyError(
                        f"unknown focused mirror input {input_id!r}"
                    ) from exc
        return tuple(dependencies)

    def decision_views(
        self,
        input_ids: tuple[str, ...],
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> dict[str, MirrorSnapshot]:
        """Read many focused inputs from one coherent canonical mirror revision.

        A portfolio decision can depend on several focused inputs. Reading those
        inputs independently permits a market update to land between reads, producing
        a composite state that never existed at one mirror revision. Capture the
        decision-eligible mirror once, then project every requested dependency from
        that immutable snapshot.
        """
        dependencies = self._batch_dependencies(input_ids)
        if not dependencies:
            return {}

        captured = self._mirror.active_view(
            as_of=as_of,
            max_age=max_age,
        )
        return {
            dependency.input_id: MirrorSnapshot(
                revision=captured.revision,
                events=tuple(
                    event
                    for event in captured.events
                    if dependency.matches(event)
                ),
            )
            for dependency in dependencies
        }

    def _coherent_routed_resync(
        self,
        dependencies: tuple[FocusedMirrorDependency, ...],
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> tuple[MirrorSnapshot, dict[str, frozenset[MirrorQuoteKey]]] | None:
        """Rebuild bounded routed keys, or require canonical selector fallback."""
        for _ in range(4):
            full = self._mirror.view()
            keys_by_input: dict[str, frozenset[MirrorQuoteKey]] = {}
            routed_key_count = 0
            overflowed = False
            for dependency in dependencies:
                keys, complete = self._bounded_matching_keys(
                    full.events,
                    dependency,
                )
                if (
                    not complete
                    or routed_key_count + len(keys) > self._max_cached_keys_total
                ):
                    overflowed = True
                    break
                keys_by_input[dependency.input_id] = frozenset(keys)
                routed_key_count += len(keys)

            if overflowed:
                with self._lock:
                    for dependency in dependencies:
                        if self._dependencies.get(dependency.input_id) != dependency:
                            raise RuntimeError(
                                "focused mirror dependency changed during routed resync"
                            )
                    for dependency in dependencies:
                        self._replace_cached_keys(
                            dependency.input_id,
                            set(),
                            complete=False,
                            revision=full.revision,
                        )
                return None

            union_keys: set[MirrorQuoteKey] = set()
            for keys in keys_by_input.values():
                union_keys.update(keys)

            captured = self._mirror.active_view_for_keys(
                union_keys,
                as_of=as_of,
                max_age=max_age,
            )
            if captured.revision != full.revision:
                continue

            with self._lock:
                for dependency in dependencies:
                    if self._dependencies.get(dependency.input_id) != dependency:
                        raise RuntimeError(
                            "focused mirror dependency changed during routed resync"
                        )
                for dependency in dependencies:
                    input_id = dependency.input_id
                    self._replace_cached_keys(
                        input_id,
                        set(keys_by_input[input_id]),
                        complete=True,
                        revision=captured.revision,
                    )
            return captured, keys_by_input

        raise RuntimeError(
            "market mirror changed continuously during routed dependency resync"
        )

    def incremental_decision_views(
        self,
        input_ids: tuple[str, ...],
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> dict[str, MirrorSnapshot]:
        """Read routed inputs only when their keysets cover the captured revision.

        Each routed keyset carries the exact mirror revision through which drained
        invalidations are known complete. If the canonical mirror advances between
        key capture and focused read, or registration published from an older cut,
        one coherent selector resync is used instead of stamping the newer revision
        onto an older incomplete key union.
        """
        if type(input_ids) is not tuple:
            raise TypeError("input_ids must be a tuple")
        normalized = tuple(self._input_id(input_id) for input_id in input_ids)
        if len(normalized) != len(set(normalized)):
            raise ValueError("input_ids must be unique")

        dependencies: list[FocusedMirrorDependency] = []
        keys_by_input: dict[str, frozenset[MirrorQuoteKey]] = {}
        revisions_by_input: dict[str, int | None] = {}
        cache_incomplete = False
        with self._lock:
            for input_id in normalized:
                try:
                    dependency = self._dependencies[input_id]
                except KeyError as exc:
                    raise KeyError(
                        f"unknown focused mirror input {input_id!r}"
                    ) from exc
                dependencies.append(dependency)
                keys_by_input[input_id] = frozenset(
                    self._matched_keys.get(input_id, set())
                )
                revisions_by_input[input_id] = self._matched_revisions.get(input_id)
                cache_incomplete = (
                    cache_incomplete or input_id in self._incomplete_keysets
                )

        if not dependencies:
            return {}

        if cache_incomplete:
            return self.decision_views(
                normalized,
                as_of=as_of,
                max_age=max_age,
            )

        union_keys: set[MirrorQuoteKey] = set()
        for keys in keys_by_input.values():
            union_keys.update(keys)

        captured = self._mirror.active_view_for_keys(
            union_keys,
            as_of=as_of,
            max_age=max_age,
        )
        dependency_tuple = tuple(dependencies)
        if any(
            revisions_by_input[dependency.input_id] != captured.revision
            for dependency in dependency_tuple
        ):
            resynced = self._coherent_routed_resync(
                dependency_tuple,
                as_of=as_of,
                max_age=max_age,
            )
            if resynced is None:
                return self.decision_views(
                    normalized,
                    as_of=as_of,
                    max_age=max_age,
                )
            captured, keys_by_input = resynced

        return {
            dependency.input_id: MirrorSnapshot(
                revision=captured.revision,
                events=tuple(
                    event
                    for event in captured.events
                    if (
                        (event.source_id, event.quote_key)
                        in keys_by_input[dependency.input_id]
                        and dependency.matches(event)
                    )
                ),
            )
            for dependency in dependency_tuple
        }

    def decision_view(
        self,
        input_id: str,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> MirrorSnapshot:
        """Read one canonical focused view without depending on invalidation drains."""
        return self.decision_views(
            (input_id,),
            as_of=as_of,
            max_age=max_age,
        )[input_id]

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
        return self.incremental_decision_views(
            (input_id,),
            as_of=as_of,
            max_age=max_age,
        )[input_id]

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
        self._mirror_revision = mirror.view().revision
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

            self._mirror_revision += 1

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
                    mirror_revision=self._mirror_revision,
                )

            count = min(max_items, len(self._dirty))
            keys = tuple(list(self._dirty)[:count])
            for key in keys:
                del self._dirty[key]
            return MirrorInvalidationBatch(
                changed_keys=keys,
                full_refresh_required=False,
                has_more=bool(self._dirty),
                mirror_revision=self._mirror_revision,
            )
