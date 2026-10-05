from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import TypeVar

from .domain import MarketEvent
from .market_mirror import MarketMirror, MirrorApplyResult, MirrorSnapshot, MirrorUpdate
from .storage import SQLiteMarketStore


MirrorQuoteKey = tuple[str, str]
MirrorRefreshIdentity = tuple[MirrorQuoteKey, int]
_StableReadT = TypeVar("_StableReadT")
_DEPENDENCY_READ_RETRY_LIMIT = 8


class FocusedMirrorDependencyChurnError(RuntimeError):
    """Raised when one focused dependency cannot be read from a stable incarnation."""


@dataclass(frozen=True, slots=True)
class MirrorInvalidationBatch:
    """Bounded downstream work derived from already-applied durable market updates.

    ``full_refresh_required`` is an explicit fail-safe fence. When it is true,
    ``changed_keys`` is intentionally empty: bounded key tracking saturated, so a
    consumer must refresh from one coherent ``MarketMirror.view()`` rather than
    pretending that an incomplete affected-key list is authoritative.

    ``semantic_refresh_keys`` is only a classification subset of ``changed_keys``.
    It preserves the already-proven distinction between a decision-causal acquisition
    that changed economic/provider state and one that only refreshed local liveness.
    ``semantic_refresh_identities`` binds every classified key to the exact
    source-local acquisition sequence; stale or noncausal classifications therefore
    cannot be reused against newer mirror truth. This metadata does not suppress
    invalidation and is not positive execution authority.
    """

    changed_keys: tuple[MirrorQuoteKey, ...]
    full_refresh_required: bool
    has_more: bool
    semantic_refresh_keys: tuple[MirrorQuoteKey, ...] = ()
    semantic_refresh_identities: tuple[MirrorRefreshIdentity, ...] = ()
    mirror_revision: int | None = None

    def __post_init__(self) -> None:
        if type(self.full_refresh_required) is not bool or type(self.has_more) is not bool:
            raise TypeError("invalidation batch flags must be booleans")
        if self.mirror_revision is not None and (
            type(self.mirror_revision) is not int or self.mirror_revision < 0
        ):
            raise ValueError("mirror_revision must be a non-negative int or None")
        if (
            type(self.changed_keys) is not tuple
            or type(self.semantic_refresh_keys) is not tuple
            or type(self.semantic_refresh_identities) is not tuple
        ):
            raise TypeError("invalidation key collections must be tuples")
        for key in (*self.changed_keys, *self.semantic_refresh_keys):
            if (
                type(key) is not tuple
                or len(key) != 2
                or type(key[0]) is not str
                or type(key[1]) is not str
                or not key[0]
                or not key[1]
            ):
                raise ValueError(
                    "invalidation keys must be non-empty (source_id, quote_key) strings"
                )
        if len(set(self.changed_keys)) != len(self.changed_keys):
            raise ValueError("changed invalidation keys must be unique")
        if len(set(self.semantic_refresh_keys)) != len(self.semantic_refresh_keys):
            raise ValueError("semantic refresh keys must be unique")

        refresh_identity_keys: list[MirrorQuoteKey] = []
        for identity in self.semantic_refresh_identities:
            if type(identity) is not tuple or len(identity) != 2:
                raise ValueError(
                    "semantic refresh identity must be ((source_id, quote_key), sequence)"
                )
            key, sequence = identity
            if (
                type(key) is not tuple
                or len(key) != 2
                or type(key[0]) is not str
                or type(key[1]) is not str
                or not key[0]
                or not key[1]
                or type(sequence) is not int
                or sequence <= 0
            ):
                raise ValueError(
                    "semantic refresh identity must bind one valid key to a positive sequence"
                )
            refresh_identity_keys.append(key)
        if len(set(refresh_identity_keys)) != len(refresh_identity_keys):
            raise ValueError("semantic refresh identities must have unique keys")
        if frozenset(refresh_identity_keys) != frozenset(self.semantic_refresh_keys):
            raise ValueError(
                "semantic refresh identities must exactly bind semantic refresh keys"
            )

        changed = frozenset(self.changed_keys)
        semantic_refresh = frozenset(self.semantic_refresh_keys)
        if not semantic_refresh.issubset(changed):
            raise ValueError(
                "semantic refresh keys must be a subset of changed invalidation keys"
            )
        if self.full_refresh_required and (
            self.changed_keys
            or self.semantic_refresh_keys
            or self.semantic_refresh_identities
            or self.has_more
        ):
            raise ValueError(
                "full-refresh invalidation must not carry bounded key state"
            )


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
        self._matched_revisions: dict[str, int] = {}
        self._dependency_revisions: dict[str, int] = {}
        self._registry_revision = 0
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
        initial_view = self._mirror.view()
        initial_keys = {
            (event.source_id, event.quote_key)
            for event in initial_view.events
            if dependency.matches(event)
        }
        with self._lock:
            if normalized_id in self._dependencies:
                raise ValueError(f"input_id {normalized_id!r} is already registered")
            self._dependencies[normalized_id] = dependency
            self._matched_keys[normalized_id] = initial_keys
            self._matched_revisions[normalized_id] = initial_view.revision
            self._registry_revision += 1
            self._dependency_revisions[normalized_id] = self._registry_revision
            dependency_revision = self._registry_revision

        # Close the mirror-view -> registry-publication race without making this
        # index a second market-state authority. If mirror truth advanced after
        # the initial view but before registration became visible, a post-publication
        # coherent catch-up observes those keys. Later advances are covered by normal
        # invalidation routing because the dependency is already registered.
        try:
            catch_up = self._mirror.view()
            if catch_up.revision != initial_view.revision:
                catch_up_keys = {
                    (event.source_id, event.quote_key)
                    for event in catch_up.events
                    if dependency.matches(event)
                }
                with self._lock:
                    if (
                        self._dependencies.get(normalized_id) == dependency
                        and self._dependency_revisions.get(normalized_id)
                        == dependency_revision
                    ):
                        self._matched_keys[normalized_id].update(catch_up_keys)
                        self._matched_revisions[normalized_id] = catch_up.revision
        except BaseException:
            # Registration is one transactional publication. If post-publication
            # catch-up cannot complete, retract only the exact incarnation created
            # by this call; never delete a concurrent replacement.
            with self._lock:
                if (
                    self._dependencies.get(normalized_id) == dependency
                    and self._dependency_revisions.get(normalized_id)
                    == dependency_revision
                ):
                    self._dependencies.pop(normalized_id, None)
                    self._matched_keys.pop(normalized_id, None)
                    self._matched_revisions.pop(normalized_id, None)
                    self._dependency_revisions.pop(normalized_id, None)
                    self._registry_revision += 1
            raise
        return dependency

    def unregister(self, input_id: str) -> bool:
        normalized_id = self._input_id(input_id)
        with self._lock:
            removed = self._dependencies.pop(normalized_id, None)
            self._matched_keys.pop(normalized_id, None)
            self._matched_revisions.pop(normalized_id, None)
            self._dependency_revisions.pop(normalized_id, None)
            if removed is not None:
                self._registry_revision += 1
            return removed is not None

    def dependency_revision(self, input_id: str) -> int:
        """Return the incarnation token for one currently registered dependency."""
        normalized_id = self._input_id(input_id)
        with self._lock:
            if normalized_id not in self._dependencies:
                raise KeyError(f"unknown focused mirror input {normalized_id!r}")
            return self._dependency_revisions[normalized_id]

    @property
    def input_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._dependencies)

    def registry_snapshot(self) -> tuple[FocusedMirrorDependency, ...]:
        """Return one atomic immutable snapshot of the focused dependency registry."""
        with self._lock:
            return tuple(self._dependencies.values())

    def registry_state_snapshot(
        self,
    ) -> tuple[tuple[FocusedMirrorDependency, int], ...]:
        """Return dependencies and their incarnation tokens from one registry lock."""
        with self._lock:
            return tuple(
                (dependency, self._dependency_revisions[input_id])
                for input_id, dependency in self._dependencies.items()
            )

    @contextmanager
    def registry_state_guard(
        self,
    ) -> Iterator[tuple[tuple[FocusedMirrorDependency, int], ...]]:
        """Hold the registry lock while a caller validates and publishes bound state."""
        with self._lock:
            yield tuple(
                (dependency, self._dependency_revisions[input_id])
                for input_id, dependency in self._dependencies.items()
            )

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
            dependency_state = tuple(
                (dependency, self._dependency_revisions[input_id])
                for input_id, dependency in self._dependencies.items()
            )
            dependencies = tuple(
                dependency for dependency, _revision in dependency_state
            )
            registry_revision = self._registry_revision

        if batch.full_refresh_required:
            captured = self._mirror.view()
            rebuilt = {
                dependency.input_id: {
                    (event.source_id, event.quote_key)
                    for event in captured.events
                    if dependency.matches(event)
                }
                for dependency in dependencies
            }
            with self._lock:
                active_dependencies = tuple(self._dependencies.values())
                for dependency, dependency_revision in dependency_state:
                    if (
                        self._dependencies.get(dependency.input_id) == dependency
                        and self._dependency_revisions.get(dependency.input_id)
                        == dependency_revision
                    ):
                        self._matched_keys[dependency.input_id] = rebuilt[
                            dependency.input_id
                        ]
                        self._matched_revisions[dependency.input_id] = captured.revision
            # Return the live registry, not the pre-snapshot registry. A dependency
            # removed while the fail-safe snapshot was captured must not escape as
            # stale pending work; a newly registered dependency is conservatively
            # affected and already owns its own registration catch-up.
            return tuple(
                dependency.input_id for dependency in active_dependencies
            )
        if not batch.changed_keys or not dependencies:
            return ()

        changed_keys = frozenset(batch.changed_keys)
        captured = self._mirror.view_for_keys(changed_keys)
        changed_events = {
            (event.source_id, event.quote_key): event
            for event in captured.events
        }
        if len(changed_events) != len(changed_keys):
            # A drained dirty key should still exist in canonical latest truth.
            # If a malformed/manual batch or concurrent invariant break violates
            # that law, missing the affected dependency would be less safe than
            # conservatively recomputing every currently registered input.
            with self._lock:
                return tuple(self._dependencies)

        affected: list[str] = []
        with self._lock:
            if self._registry_revision != registry_revision:
                # The drained invalidation was classified against a registry that
                # ceased to exist while canonical mirror truth was being read.
                # Returning every live input is conservative but prevents a same-ID
                # replacement/reincarnation from silently consuming the batch.
                return tuple(self._dependencies)
            for dependency, dependency_revision in dependency_state:
                if (
                    self._dependencies.get(dependency.input_id) != dependency
                    or self._dependency_revisions.get(dependency.input_id)
                    != dependency_revision
                ):
                    continue
                matched = self._matched_keys.setdefault(dependency.input_id, set())
                dependency_affected = False
                for key in batch.changed_keys:
                    event = changed_events[key]
                    previously_matched = key in matched
                    currently_matches = dependency.matches(event)
                    if currently_matches:
                        matched.add(key)
                    elif previously_matched:
                        matched.discard(key)
                    if previously_matched or currently_matches:
                        dependency_affected = True
                if dependency_affected:
                    affected.append(dependency.input_id)
            if (
                not batch.has_more
                and batch.mirror_revision is not None
                and captured.revision == batch.mirror_revision
            ):
                for dependency, dependency_revision in dependency_state:
                    if (
                        self._dependencies.get(dependency.input_id) == dependency
                        and self._dependency_revisions.get(dependency.input_id)
                        == dependency_revision
                    ):
                        self._matched_revisions[dependency.input_id] = captured.revision
        return tuple(affected)

    def semantic_refresh_only_inputs(
        self,
        batch: MirrorInvalidationBatch,
    ) -> tuple[str, ...]:
        """Return inputs touched only by proven semantic-refresh keys in this batch.

        This is routing metadata only. It does not make cached intents executable
        against a newer acquisition and must not be used to suppress recomputation
        until the consumer independently rebinds every evidence authority.
        """

        if not isinstance(batch, MirrorInvalidationBatch):
            raise TypeError("batch must be a MirrorInvalidationBatch")
        if batch.full_refresh_required or not batch.semantic_refresh_keys:
            return ()

        changed_keys = frozenset(batch.changed_keys)
        refresh_keys = frozenset(batch.semantic_refresh_keys)
        if not refresh_keys.issubset(changed_keys):
            raise ValueError(
                "semantic refresh keys must be a subset of changed invalidation keys"
            )
        material_keys = changed_keys - refresh_keys

        with self._lock:
            dependencies = tuple(self._dependencies.values())
            registry_revision = self._registry_revision

        captured = self._mirror.view_for_keys(changed_keys, _causal_only=True)
        events = {
            (event.source_id, event.quote_key): event
            for event in captured.events
        }
        if len(events) != len(changed_keys):
            # Positive refresh-only classification must fail closed when one coherent
            # mirror revision cannot resolve every changed identity in the batch.
            return ()
        refresh_sequences = dict(batch.semantic_refresh_identities)
        if any(
            events[key].sequence != refresh_sequences[key]
            for key in refresh_keys
        ):
            # A later acquisition already advanced this key after the batch was
            # drained. Stale refresh-only provenance must never relabel newer state.
            return ()

        refresh_only: list[str] = []
        for dependency in dependencies:
            refresh_matches = any(
                dependency.matches(events[key]) for key in refresh_keys
            )
            if not refresh_matches:
                continue
            material_matches = any(
                dependency.matches(events[key]) for key in material_keys
            )
            if not material_matches:
                refresh_only.append(dependency.input_id)

        with self._lock:
            if self._registry_revision != registry_revision:
                # Positive optimization metadata must never cross a concurrent
                # dependency-registration mutation. Ordinary invalidation remains
                # conservative; only the optional refresh-only classification is lost.
                return ()
            if any(
                self._dependencies.get(dependency.input_id) != dependency
                for dependency in dependencies
            ):
                return ()
        return tuple(refresh_only)

    @staticmethod
    def _selectors(dependency: FocusedMirrorDependency) -> dict[str, frozenset[str] | None]:
        return {
            "source_ids": dependency.source_ids,
            "sports": dependency.sports,
            "event_ids": dependency.event_ids,
            "market_ids": dependency.market_ids,
            "selection_ids": dependency.selection_ids,
        }

    def _stable_dependency_read(
        self,
        input_id: str,
        reader: Callable[[FocusedMirrorDependency], _StableReadT],
    ) -> _StableReadT:
        """Run one selector-based read against a stable registry incarnation."""
        normalized_id = self._input_id(input_id)
        for _attempt in range(_DEPENDENCY_READ_RETRY_LIMIT):
            with self._lock:
                try:
                    dependency = self._dependencies[normalized_id]
                    dependency_revision = self._dependency_revisions[normalized_id]
                except KeyError as exc:
                    raise KeyError(
                        f"unknown focused mirror input {normalized_id!r}"
                    ) from exc

            result = reader(dependency)

            with self._lock:
                current_dependency = self._dependencies.get(normalized_id)
                if current_dependency is None:
                    raise KeyError(
                        f"unknown focused mirror input {normalized_id!r}"
                    )
                if (
                    current_dependency == dependency
                    and self._dependency_revisions.get(normalized_id)
                    == dependency_revision
                ):
                    return result
            # The target dependency changed while the dependency-bearing read was
            # in flight. Retry against its now-authoritative selectors rather than
            # returning a result from an incarnation that no longer exists. Churn
            # in unrelated registrations does not invalidate this selector read.
        raise FocusedMirrorDependencyChurnError(
            "focused mirror dependency changed continuously during stable read"
        )

    def _stable_live_decision_view(
        self,
        input_id: str,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> MirrorSnapshot:
        """Read selector-based live truth only from a stable registry incarnation."""
        return self._stable_dependency_read(
            input_id,
            lambda dependency: self._mirror.active_view(
                as_of=as_of,
                max_age=max_age,
                **self._selectors(dependency),
            ),
        )

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
        return self._stable_dependency_read(
            input_id,
            lambda dependency: self._mirror.causal_view(
                **self._selectors(dependency)
            ),
        )

    def requires_current_history_fallback(
        self,
        input_id: str,
        *,
        as_of: datetime,
    ) -> bool:
        """Return whether latest-only state hides an earlier causally available value."""

        boundary, _ = MarketMirror._decision_boundary(
            as_of=as_of,
            max_age=timedelta(0),
        )
        current = self.causal_view(input_id)
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

        return self._stable_dependency_read(
            input_id,
            lambda dependency: MarketMirror.current_history_view_from_store(
                store,
                as_of=as_of,
                max_age=max_age,
                **self._selectors(dependency),
            ),
        )

    def _decision_state_for_dependency(
        self,
        dependency: FocusedMirrorDependency,
        events_with_generation: tuple[tuple[MarketEvent, int], ...],
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> tuple[MirrorSnapshot, datetime | None]:
        """Resolve one dependency from a caller-owned storage-proven snapshot."""

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
            return event.dedupe_key

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

    def decision_state_from_proven_history(
        self,
        input_id: str,
        events_with_generation: tuple[tuple[MarketEvent, int], ...],
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> tuple[MirrorSnapshot, datetime | None]:
        """Resolve one input from a caller-owned snapshot already proven by storage."""

        return self._stable_dependency_read(
            input_id,
            lambda dependency: self._decision_state_for_dependency(
                dependency,
                events_with_generation,
                as_of=as_of,
                max_age=max_age,
            ),
        )

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
        return self._stable_live_decision_view(
            input_id,
            as_of=as_of,
            max_age=max_age,
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
        normalized_id = self._input_id(input_id)
        with self._lock:
            try:
                dependency = self._dependencies[normalized_id]
                dependency_revision = self._dependency_revisions[normalized_id]
            except KeyError as exc:
                raise KeyError(
                    f"unknown focused mirror input {normalized_id!r}"
                ) from exc
            keys = tuple(sorted(self._matched_keys.get(normalized_id, set())))
            index_revision = self._matched_revisions.get(normalized_id)

        bounded = self._mirror.active_view_for_keys(
            keys,
            as_of=as_of,
            max_age=max_age,
        )

        with self._lock:
            current_dependency = self._dependencies.get(normalized_id)
            current_dependency_revision = self._dependency_revisions.get(normalized_id)
        if current_dependency is None:
            raise KeyError(f"unknown focused mirror input {normalized_id!r}")
        if (
            current_dependency != dependency
            or current_dependency_revision != dependency_revision
        ):
            # Never return a bounded snapshot captured against selectors that ceased
            # to be authoritative while the mirror read was in flight. The fallback
            # itself is revision-stamped because the registry can change again while
            # the full selector-based mirror read is in flight.
            return self._stable_live_decision_view(
                normalized_id,
                as_of=as_of,
                max_age=max_age,
            )
        if index_revision is not None and bounded.revision == index_revision:
            return bounded

        # The bounded identity index is only an optimization. If mirror truth has
        # advanced beyond the revision proven by routed invalidations, fall back to
        # the canonical selector-based focused view so a newly matching quote cannot
        # disappear from a live decision merely because routing raced this read.
        # Registry truth can also change during that full read, so use the same
        # revision-stamped retry fence as the explicit replacement path above.
        return self._stable_live_decision_view(
            normalized_id,
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
        return self._stable_dependency_read(
            input_id,
            lambda dependency: MarketMirror.replay_view_from_store(
                store,
                as_of=as_of,
                max_age=max_age,
                **self._selectors(dependency),
            ),
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
        self._semantic_refresh: dict[MirrorQuoteKey, int] = {}
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
            if result.status not in {
                MirrorUpdate.APPLIED,
                MirrorUpdate.SEMANTIC_REFRESH,
            }:
                return result

            if self._full_refresh_required:
                return result

            key = (result.source_id, result.quote_key)
            if key in self._dirty:
                # Coalescing must never relabel a batch containing a material update
                # or a loss of decision-causal authority as refresh-only. Repeated
                # causal refreshes retain refresh-only status and advance provenance.
                if result.status is MirrorUpdate.APPLIED or not decision_causal:
                    self._semantic_refresh.pop(key, None)
                elif (
                    result.status is MirrorUpdate.SEMANTIC_REFRESH
                    and key in self._semantic_refresh
                ):
                    self._semantic_refresh[key] = event.sequence
                return result

            if len(self._dirty) >= self._max_dirty_keys:
                # Never publish a partial affected-key list as complete truth.
                # The mirror already contains this update, so degrade to one
                # coherent full refresh rather than dropping durable state.
                self._dirty.clear()
                self._semantic_refresh.clear()
                self._full_refresh_required = True
                return result

            self._dirty[key] = None
            if (
                result.status is MirrorUpdate.SEMANTIC_REFRESH
                and decision_causal
            ):
                self._semantic_refresh[key] = event.sequence
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
            mirror_revision = self._mirror.view_for_keys(()).revision
            if self._full_refresh_required:
                self._full_refresh_required = False
                self._dirty.clear()
                self._semantic_refresh.clear()
                return MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=True,
                    has_more=False,
                    semantic_refresh_keys=(),
                    semantic_refresh_identities=(),
                    mirror_revision=mirror_revision,
                )

            count = min(max_items, len(self._dirty))
            keys = tuple(list(self._dirty)[:count])
            semantic_refresh_keys = tuple(
                key for key in keys if key in self._semantic_refresh
            )
            semantic_refresh_identities = tuple(
                (key, self._semantic_refresh[key])
                for key in semantic_refresh_keys
            )
            for key in keys:
                del self._dirty[key]
                self._semantic_refresh.pop(key, None)
            return MirrorInvalidationBatch(
                changed_keys=keys,
                full_refresh_required=False,
                has_more=bool(self._dirty),
                semantic_refresh_keys=semantic_refresh_keys,
                semantic_refresh_identities=semantic_refresh_identities,
                mirror_revision=mirror_revision,
            )
