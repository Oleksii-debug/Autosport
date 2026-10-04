from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

from .domain import MarketEvent
from .market_mirror import (
    MarketMirror,
    MarketMirrorRevisionChanged,
    MirrorApplyResult,
    MirrorSnapshot,
    MirrorUpdate,
)
from .storage import SQLiteMarketStore


MirrorQuoteKey = tuple[str, str]


class FocusedMirrorRegistryChanged(RuntimeError):
    """The focused dependency registry moved across an economic decision cut."""


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
        self._publication_input_guard: tuple[str, ...] | None = None
        # Monotonic generation for selector/key-routing state. Mirror revision alone
        # cannot detect the short interval after an invalidation batch is drained
        # but before its changed keys have been routed into _matched_keys.
        self._routing_revision = 0

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
        # Registration must bind its initial matching-key set to one exact mirror
        # revision. Otherwise an update can land after snapshot capture, be drained
        # before this dependency exists, and leave the new input permanently missing
        # a current matching quote until some unrelated later invalidation.
        for _attempt in range(8):
            captured = self._mirror.view()
            initial_keys = {
                (event.source_id, event.quote_key)
                for event in captured.events
                if dependency.matches(event)
            }
            try:
                # Canonical lock order is mirror -> dependency registry, matching the
                # live decision publication boundary.
                with self._mirror.hold_revision(captured.revision):
                    with self._lock:
                        if self._publication_input_guard is not None:
                            raise FocusedMirrorRegistryChanged(
                                "focused mirror dependency mutation is blocked "
                                "during decision publication"
                            )
                        if normalized_id in self._dependencies:
                            raise ValueError(
                                f"input_id {normalized_id!r} is already registered"
                            )
                        self._dependencies[normalized_id] = dependency
                        self._matched_keys[normalized_id] = initial_keys
                        self._routing_revision += 1
                    return dependency
            except MarketMirrorRevisionChanged:
                continue
        raise FocusedMirrorRegistryChanged(
            "market mirror did not stabilize during focused dependency registration"
        )

    def unregister(self, input_id: str) -> bool:
        normalized_id = self._input_id(input_id)
        with self._lock:
            if self._publication_input_guard is not None:
                raise FocusedMirrorRegistryChanged(
                    "focused mirror dependency mutation is blocked during decision publication"
                )
            removed = self._dependencies.pop(normalized_id, None)
            self._matched_keys.pop(normalized_id, None)
            if removed is not None:
                self._routing_revision += 1
            return removed is not None

    @property
    def input_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._dependencies))

    @property
    def routing_revision(self) -> int:
        """Return the exact dependency/key-routing generation."""
        with self._lock:
            return self._routing_revision

    @contextmanager
    def hold_input_ids(
        self,
        expected_input_ids: tuple[str, ...],
        *,
        expected_routing_revision: int | None = None,
    ) -> Iterator[None]:
        """Linearize a short publication step against one exact dependency registry."""

        if type(expected_input_ids) is not tuple or any(
            type(input_id) is not str for input_id in expected_input_ids
        ):
            raise TypeError("expected_input_ids must be a tuple of strings")
        expected = tuple(sorted(expected_input_ids))
        if len(expected) != len(set(expected)):
            raise ValueError("expected_input_ids must be unique")
        if expected_routing_revision is not None and (
            type(expected_routing_revision) is not int
            or expected_routing_revision < 0
        ):
            raise TypeError(
                "expected_routing_revision must be a non-negative integer or None"
            )
        with self._lock:
            if self._publication_input_guard is not None:
                raise FocusedMirrorRegistryChanged(
                    "focused mirror dependency publication guard is already active"
                )
            current = tuple(sorted(self._dependencies))
            if current != expected:
                raise FocusedMirrorRegistryChanged(
                    "focused mirror dependency registry changed before decision publication"
                )
            if (
                expected_routing_revision is not None
                and self._routing_revision != expected_routing_revision
            ):
                raise FocusedMirrorRegistryChanged(
                    "focused mirror dependency routing changed before decision publication"
                )
            self._publication_input_guard = expected
            try:
                yield
                if tuple(sorted(self._dependencies)) != expected:
                    raise FocusedMirrorRegistryChanged(
                        "focused mirror dependency registry changed during decision publication"
                    )
                if (
                    expected_routing_revision is not None
                    and self._routing_revision != expected_routing_revision
                ):
                    raise FocusedMirrorRegistryChanged(
                        "focused mirror dependency routing changed during decision publication"
                    )
            finally:
                self._publication_input_guard = None

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
            dependencies = tuple(
                sorted(
                    self._dependencies.values(),
                    key=lambda dependency: dependency.input_id,
                )
            )

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
                routing_changed = False
                for dependency in dependencies:
                    if self._dependencies.get(dependency.input_id) == dependency:
                        rebuilt_keys = rebuilt[dependency.input_id]
                        if self._matched_keys.get(dependency.input_id, set()) != rebuilt_keys:
                            self._matched_keys[dependency.input_id] = rebuilt_keys
                            routing_changed = True
                if routing_changed:
                    self._routing_revision += 1
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
            routing_changed = False
            for dependency in dependencies:
                if self._dependencies.get(dependency.input_id) != dependency:
                    continue
                matched = self._matched_keys.setdefault(dependency.input_id, set())
                dependency_affected = False
                for event in changed_events:
                    if not dependency.matches(event):
                        continue
                    key = (event.source_id, event.quote_key)
                    if key not in matched:
                        matched.add(key)
                        routing_changed = True
                    dependency_affected = True
                if dependency_affected:
                    affected.append(dependency.input_id)
            if routing_changed:
                self._routing_revision += 1
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

    def coherent_decision_views(
        self,
        input_ids: Iterable[str],
        *,
        as_of: datetime,
        max_age: timedelta,
        incremental: bool = True,
    ) -> dict[str, MirrorSnapshot]:
        """Read several focused inputs from one exact canonical mirror revision.

        Incremental mode reads only the union of quote identities already maintained
        by invalidation routing. Full mode takes one canonical whole-mirror active
        view and filters that immutable cut. Either mode captures market values only
        once, so callers cannot accidentally compose input A from revision N with
        input B from revision N+1.
        """
        if isinstance(input_ids, (str, bytes)):
            raise TypeError("input_ids must be an iterable of input_id strings")
        try:
            requested = tuple(input_ids)
        except TypeError as exc:
            raise TypeError("input_ids must be an iterable of input_id strings") from exc
        if len(requested) != len(set(requested)):
            raise ValueError("input_ids must be unique")

        dependencies: list[FocusedMirrorDependency] = []
        keys: set[MirrorQuoteKey] = set()
        with self._lock:
            captured_routing_revision = self._routing_revision
            for input_id in requested:
                normalized_id = self._input_id(input_id)
                try:
                    dependency = self._dependencies[normalized_id]
                except KeyError as exc:
                    raise KeyError(
                        f"unknown focused mirror input {normalized_id!r}"
                    ) from exc
                dependencies.append(dependency)
                if incremental:
                    keys.update(self._matched_keys.get(normalized_id, set()))

        if incremental:
            captured = self._mirror.active_view_for_keys(
                tuple(sorted(keys)),
                as_of=as_of,
                max_age=max_age,
            )
        else:
            captured = self._mirror.active_view(
                as_of=as_of,
                max_age=max_age,
            )

        with self._lock:
            if any(
                self._dependencies.get(dependency.input_id) != dependency
                for dependency in dependencies
            ):
                raise FocusedMirrorRegistryChanged(
                    "focused mirror dependency registry changed during coherent capture"
                )
            if self._routing_revision != captured_routing_revision:
                raise FocusedMirrorRegistryChanged(
                    "focused mirror dependency routing changed during coherent capture"
                )

        return {
            dependency.input_id: MirrorSnapshot(
                revision=captured.revision,
                events=tuple(
                    MarketMirror._snapshot_event(event)
                    for event in captured.events
                    if dependency.matches(event)
                ),
            )
            for dependency in dependencies
        }

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
        # A live subscriber failure happens only after SQLite commit. Preserve the
        # exact already-durable event so the next canonical poll can repair mirror
        # state before any newer provider batch is allowed to advance.
        self._pending_recovery: list[MarketEvent] = []
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

    @property
    def pending_recovery_count(self) -> int:
        """Return durable subscriber deliveries still missing from mirror state."""
        with self._lock:
            return len(self._pending_recovery)

    def _record_invalidation_locked(self, result: MirrorApplyResult) -> None:
        if result.status is not MirrorUpdate.APPLIED:
            return

        if self._full_refresh_required:
            return

        key = (result.source_id, result.quote_key)
        if key in self._dirty:
            return

        if len(self._dirty) >= self._max_dirty_keys:
            # Never publish a partial affected-key list as complete truth.
            # The mirror already contains this update, so degrade to one
            # coherent full refresh rather than dropping durable state.
            self._dirty.clear()
            self._full_refresh_required = True
            return

        self._dirty[key] = None

    def accept_persisted(
        self,
        event: MarketEvent,
        *,
        _snapshot_event=MarketMirror._snapshot_event,
        _apply=MarketMirror.apply,
    ) -> MirrorApplyResult:
        """Apply one already-durable event and record its affected quote if material.

        The tracker lock covers both mirror mutation and invalidation publication so
        a concurrent ``drain`` cannot observe an applied mirror update before its
        downstream invalidation state has been established.
        """
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")

        with self._lock:
            try:
                result = _apply(self._mirror, event)
            except Exception:
                # MarketEventBus invokes this callback only after the live receipt
                # transaction commits. Keep an owned canonical snapshot before
                # surfacing the delivery failure so a later poll can repair the
                # non-durable mirror without replaying or refetching the provider.
                self._pending_recovery.append(_snapshot_event(event))
                raise
            self._record_invalidation_locked(result)
            return result

    def reconcile_pending(
        self,
        *,
        _apply=MarketMirror.apply,
    ) -> tuple[MirrorApplyResult, ...]:
        """Repair post-commit subscriber failures before admitting newer live input.

        Recovery is strictly in original delivery order. A still-failing head remains
        queued and the exception is surfaced, so callers cannot skip an unresolved
        durable event and continue making decisions from a newer partial mirror.
        Successful material repairs enter the same bounded invalidation protocol as
        ordinary subscriber delivery.
        """
        recovered: list[MirrorApplyResult] = []
        with self._lock:
            while self._pending_recovery:
                event = self._pending_recovery[0]
                result = _apply(self._mirror, event)
                self._record_invalidation_locked(result)
                self._pending_recovery.pop(0)
                recovered.append(result)
        return tuple(recovered)

    def drain_and_route(
        self,
        dependencies: FocusedMirrorDependencyIndex,
        *,
        max_items: int = 250,
    ) -> tuple[MirrorInvalidationBatch, tuple[str, ...]]:
        """Atomically route one bounded dirty batch before consuming its keys.

        Economic consumers must not expose a state where canonical mirror truth has
        advanced, the dirty key has been destructively drained, but the focused
        dependency index still reflects the older key set. Holding the invalidation
        lock across routing also prevents a concurrent accept from landing between
        the routed batch and its consume point. If routing raises, the dirty state is
        retained for a later retry.
        """
        if not isinstance(dependencies, FocusedMirrorDependencyIndex):
            raise TypeError("dependencies must be a FocusedMirrorDependencyIndex")
        if dependencies._mirror is not self._mirror:
            raise ValueError("dependencies must index this invalidation buffer mirror")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")

        with self._lock:
            if self._full_refresh_required:
                batch = MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=True,
                    has_more=False,
                )
                affected = dependencies.affected_inputs(batch)
                self._full_refresh_required = False
                self._dirty.clear()
                return batch, affected

            count = min(max_items, len(self._dirty))
            keys = tuple(list(self._dirty)[:count])
            batch = MirrorInvalidationBatch(
                changed_keys=keys,
                full_refresh_required=False,
                has_more=len(self._dirty) > count,
            )
            affected = dependencies.affected_inputs(batch)
            for key in keys:
                self._dirty.pop(key, None)
            return batch, affected

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
