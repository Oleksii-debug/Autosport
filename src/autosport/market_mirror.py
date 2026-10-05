from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock

from .domain import MarketEvent, _quote_identity
from .market_state_identity import MarketStateIdentityError, same_semantic_market_state
from .storage import SQLiteMarketStore, _timezone_aware_instant


class MirrorUpdate(str, Enum):
    APPLIED = "applied"
    SEMANTIC_REFRESH = "semantic_refresh"
    DUPLICATE = "duplicate"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class MirrorApplyResult:
    status: MirrorUpdate
    source_id: str
    quote_key: str
    previous_sequence: int | None
    current_sequence: int


@dataclass(frozen=True, slots=True)
class MirrorSnapshot:
    """One coherent point-in-time mirror view for decision consumers."""

    revision: int
    events: tuple[MarketEvent, ...]


class MarketMirror:
    """Deterministic in-memory mirror for normalized market quotes.

    The mirror is deliberately presentation- and provider-adapter-neutral: it stores
    canonical ``MarketEvent`` values and enforces source-local ordering. Authoritative
    persistence, provider polling and portfolio/economic decisions remain outside this
    boundary.
    """

    _DECISION_ELIGIBLE_STATUSES = frozenset({"open"})

    def __init__(self) -> None:
        self._latest: dict[tuple[str, str], MarketEvent] = {}
        # Keys may exist only as sealed generation-zero migration baseline. Those
        # values remain audit-visible and preserve provider sequence ordering, but
        # they cannot authorize economic decisions until a positive product-issued
        # append becomes the current event for that key.
        self._decision_causal_keys: set[tuple[str, str]] = set()
        self._revision = 0
        self._lock = RLock()

    @staticmethod
    def _key(event: MarketEvent) -> tuple[str, str]:
        # Include source identity so two providers using the same local IDs cannot
        # overwrite one another's state.
        return (event.source_id, event.quote_key)

    @staticmethod
    def _snapshot_event(event: MarketEvent) -> MarketEvent:
        """Own an independent canonical value snapshot, including nested metadata."""
        return MarketEvent.from_dict(event.to_dict())

    @staticmethod
    def _same_sequence_payload(left: MarketEvent, right: MarketEvent) -> bool:
        """Compare provider payload truth while ignoring local receipt clocks.

        ``observed_ts`` and ``ingest_ts`` are local process timestamps. A retry or
        replay of one provider sequence may legitimately receive different local
        timestamps, and canonical storage uses the same identity rule. Provider time
        (``source_ts``) and every economic/provider/provenance field remain part of the
        conflict check.
        """
        left_payload = left.to_dict()
        right_payload = right.to_dict()
        for local_clock in ("observed_ts", "ingest_ts"):
            left_payload.pop(local_clock, None)
            right_payload.pop(local_clock, None)
        return left_payload == right_payload

    @staticmethod
    def _utc_timestamp(value: str) -> datetime | None:
        """Parse one provider/observation timestamp, failing closed on bad input."""
        try:
            parsed = _timezone_aware_instant(value, "timestamp")
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _selector(
        values: str | Iterable[str] | None,
        *,
        name: str,
    ) -> frozenset[str] | None:
        """Normalize a focused-view selector without treating one ID as characters."""
        if values is None:
            return None
        if isinstance(values, str):
            selected = frozenset({values})
        else:
            try:
                selected = frozenset(values)
            except TypeError as exc:
                raise TypeError(f"{name} must be a string or iterable of strings") from exc
        if any(not isinstance(value, str) or not value for value in selected):
            raise ValueError(f"{name} entries must be non-empty strings")
        return selected

    @staticmethod
    def _quote_key_set(
        keys: Iterable[tuple[str, str]],
    ) -> frozenset[tuple[str, str]]:
        if isinstance(keys, (str, bytes)):
            raise TypeError("keys must be an iterable of (source_id, quote_key) tuples")
        try:
            values = tuple(keys)
        except TypeError as exc:
            raise TypeError(
                "keys must be an iterable of (source_id, quote_key) tuples"
            ) from exc
        normalized: set[tuple[str, str]] = set()
        for value in values:
            if type(value) is not tuple or len(value) != 2:
                raise ValueError("mirror key must be a (source_id, quote_key) tuple")
            source_id, quote_key = value
            if type(source_id) is not str or not source_id or source_id.strip() != source_id:
                raise ValueError("mirror key source_id must be a non-empty trimmed string")
            if type(quote_key) is not str or not quote_key or quote_key.strip() != quote_key:
                raise ValueError("mirror key quote_key must be a non-empty trimmed string")
            normalized.add((source_id, quote_key))
        return frozenset(normalized)

    @staticmethod
    def _decision_boundary(*, as_of: datetime, max_age: timedelta) -> tuple[datetime, timedelta]:
        if not isinstance(as_of, datetime):
            raise TypeError("as_of must be a datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(max_age, timedelta):
            raise TypeError("max_age must be a timedelta")
        if max_age < timedelta(0):
            raise ValueError("max_age must be non-negative")
        return as_of.astimezone(timezone.utc), max_age

    @classmethod
    def _event_causal_times(
        cls,
        event: MarketEvent,
    ) -> tuple[datetime, datetime, datetime] | None:
        """Return normalized source/observed/ingest clocks when all are usable."""

        source_time = cls._utc_timestamp(event.source_ts or event.observed_ts)
        observed_time = cls._utc_timestamp(event.observed_ts)
        ingest_time = cls._utc_timestamp(event.ingest_ts)
        if source_time is None or observed_time is None or ingest_time is None:
            return None
        return source_time, observed_time, ingest_time

    @classmethod
    def _event_causally_available(
        cls,
        event: MarketEvent,
        *,
        boundary: datetime,
    ) -> bool:
        """Return whether all causal clocks make one event usable at boundary."""

        causal_times = cls._event_causal_times(event)
        return causal_times is not None and all(
            timestamp <= boundary for timestamp in causal_times
        )

    @classmethod
    def _decision_visible_event(
        cls,
        event: MarketEvent,
        *,
        boundary: datetime,
        max_age: timedelta,
    ) -> bool:
        """Require provider freshness and local causal availability at one cutoff."""

        if event.status not in cls._DECISION_ELIGIBLE_STATUSES:
            return False
        if not cls._event_causally_available(event, boundary=boundary):
            return False
        source_time = cls._utc_timestamp(event.source_ts or event.observed_ts)
        if source_time is None:
            return False
        age = boundary - source_time
        return timedelta(0) <= age <= max_age

    def _apply_with_causal_authority(
        self,
        event: MarketEvent,
        *,
        decision_causal: bool,
    ) -> MirrorApplyResult:
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")
        if type(decision_causal) is not bool:
            raise TypeError("decision_causal must be a bool")

        key = self._key(event)
        with self._lock:
            previous = self._latest.get(key)
            if previous is None:
                self._latest[key] = self._snapshot_event(event)
                if decision_causal:
                    self._decision_causal_keys.add(key)
                else:
                    self._decision_causal_keys.discard(key)
                self._revision += 1
                return MirrorApplyResult(
                    MirrorUpdate.APPLIED,
                    event.source_id,
                    event.quote_key,
                    None,
                    event.sequence,
                )

            if event.sequence < previous.sequence:
                return MirrorApplyResult(
                    MirrorUpdate.STALE,
                    event.source_id,
                    event.quote_key,
                    previous.sequence,
                    previous.sequence,
                )

            if event.sequence == previous.sequence:
                if self._same_sequence_payload(event, previous):
                    return MirrorApplyResult(
                        MirrorUpdate.DUPLICATE,
                        event.source_id,
                        event.quote_key,
                        previous.sequence,
                        previous.sequence,
                    )
                raise ValueError(
                    "conflicting MarketEvent payload reused an existing source-local sequence"
                )

            try:
                semantic_refresh = same_semantic_market_state(previous, event)
            except MarketStateIdentityError:
                # A malformed/unsupported claimed semantic contract cannot mint
                # duplicate suppression. Preserve the durable acquisition as an
                # ordinary material transition so restart/live reconstruction
                # remains available while downstream work is conservatively invalidated.
                semantic_refresh = False
            self._latest[key] = self._snapshot_event(event)
            if decision_causal:
                self._decision_causal_keys.add(key)
            else:
                self._decision_causal_keys.discard(key)
            self._revision += 1
            return MirrorApplyResult(
                (
                    MirrorUpdate.SEMANTIC_REFRESH
                    if semantic_refresh
                    else MirrorUpdate.APPLIED
                ),
                event.source_id,
                event.quote_key,
                previous.sequence,
                event.sequence,
            )

    def apply(self, event: MarketEvent) -> MirrorApplyResult:
        """Apply one live/product-issued event iff it advances source-local state.

        Public apply calls are decision-causal by construction: production subscribers
        receive only events accepted by SQLiteMarketStore as positive durable appends.
        Generation-zero migration state is loaded through the private provenance-aware
        restoration path instead.
        """

        return self._apply_with_causal_authority(
            event,
            decision_causal=True,
        )

    def persist_and_apply(
        self,
        store: SQLiteMarketStore,
        event: MarketEvent,
    ) -> MirrorApplyResult:
        """Durably record one provider observation before mutating live mirror state.

        The append-only ``SQLiteMarketStore`` remains the durable authority. Its append
        happens first, so a persistence/validation/conflict failure cannot leave the
        process exposing an update that cannot be reconstructed after restart. Stale
        but valid provider observations may still be retained in history for audit;
        ``apply`` then keeps the live source-local projection monotonic.
        """
        if not isinstance(store, SQLiteMarketStore):
            raise TypeError("store must be a SQLiteMarketStore")
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")

        admitted_event = self._snapshot_event(event)
        prior = self.event_for_quote_key(
            admitted_event.source_id,
            admitted_event.quote_key,
        )
        accepted = store.append_batch_accepted((admitted_event,))
        if accepted:
            if len(accepted) != 1:
                raise RuntimeError("single market append returned invalid accepted cardinality")
            # Apply the exact canonical value snapshot that storage admitted, not the
            # caller-owned object. Nested MarketEvent metadata is mutable even though
            # the dataclass is frozen; this closes SQLite-COMMIT -> mirror-apply TOCTOU.
            return self.apply(accepted[0])

        # If this mirror already reached this sequence (or a later one), applying a
        # storage duplicate cannot advance live state: same-sequence retries remain
        # idempotent and lower sequences remain stale. Avoid an O(history) trusted
        # reread on the ordinary duplicate-poll path.
        if prior is not None and prior.sequence >= admitted_event.sequence:
            return self.apply(admitted_event)

        # Storage intentionally treats a retry of one source-local sequence as the
        # same provider observation even when local receipt clocks changed. On a
        # fresh/incomplete mirror, applying the caller's retry object would expose
        # local timestamps that are not the durable canonical history. Re-read the
        # independently trusted persisted event before mutating live state.
        canonical_with_generation = next(
            (
                (persisted, append_generation)
                for persisted, append_generation in store.events_with_append_generation(
                    admitted_event.event_id
                )
                if persisted.dedupe_key == admitted_event.dedupe_key
            ),
            None,
        )
        if canonical_with_generation is None:
            raise RuntimeError(
                "duplicate market event disappeared from canonical history"
            )
        canonical, append_generation = canonical_with_generation
        # A storage duplicate can refer to sealed generation-zero migration history.
        # Reconstructing a fresh/incomplete mirror must preserve that row as an
        # audit/sequence fence without laundering it into decision-causal live state.
        return self._apply_with_causal_authority(
            canonical,
            decision_causal=append_generation > 0,
        )

    def view(
        self,
        *,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
        _causal_only: bool = False,
    ) -> MirrorSnapshot:
        """Capture one coherent revision and optionally filter it for a consumer.

        Filtering happens only over isolated values captured from the canonical mirror;
        it never creates a second mutable market-state authority. The revision lets a
        decision consumer detect whether two requested views came from the same mirror
        state while an updater is active.
        """
        selected_sources = self._selector(source_ids, name="source_ids")
        selected_sports = self._selector(sports, name="sports")
        selected_events = self._selector(event_ids, name="event_ids")
        selected_markets = self._selector(market_ids, name="market_ids")
        selected_selections = self._selector(selection_ids, name="selection_ids")
        if type(_causal_only) is not bool:
            raise TypeError("_causal_only must be a bool")

        with self._lock:
            revision = self._revision
            events = tuple(
                self._snapshot_event(event)
                for key, event in sorted(self._latest.items(), key=lambda item: item[0])
                if not _causal_only or key in self._decision_causal_keys
            )

        filtered = tuple(
            event
            for event in events
            if (selected_sources is None or event.source_id in selected_sources)
            and (selected_sports is None or event.sport in selected_sports)
            and (selected_events is None or event.event_id in selected_events)
            and (selected_markets is None or event.market_id in selected_markets)
            and (
                selected_selections is None
                or event.selection_id in selected_selections
            )
        )
        return MirrorSnapshot(revision=revision, events=filtered)

    def causal_view(
        self,
        *,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
    ) -> MirrorSnapshot:
        """Return product-issued current state without freshness/status filtering.

        This is the operator/current-state counterpart to active_view: it excludes
        sealed generation-zero migration rows from live product truth while retaining
        positive durable rows even when they are stale, closed, or otherwise not
        decision-eligible. Use view()/snapshot() when raw audit/order state is required.
        """

        return self.view(
            source_ids=source_ids,
            sports=sports,
            event_ids=event_ids,
            market_ids=market_ids,
            selection_ids=selection_ids,
            _causal_only=True,
        )

    def active_view(
        self,
        *,
        as_of: datetime,
        max_age: timedelta,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
    ) -> MirrorSnapshot:
        """Return one revision-bearing focused view safe for decision consumption.

        Identity selectors and freshness/status fencing are applied to the same captured
        mirror revision. Unknown/inactive, future, over-age or malformed observations
        fail closed while remaining available through ``view``/``snapshot`` for audit.
        ``source_ts`` is preferred over the local observation clock when available.
        """
        boundary, age_limit = self._decision_boundary(as_of=as_of, max_age=max_age)
        captured = self.causal_view(
            source_ids=source_ids,
            sports=sports,
            event_ids=event_ids,
            market_ids=market_ids,
            selection_ids=selection_ids,
        )
        eligible = tuple(
            event
            for event in captured.events
            if self._decision_visible_event(
                event,
                boundary=boundary,
                max_age=age_limit,
            )
        )
        return MirrorSnapshot(revision=captured.revision, events=eligible)

    def event_for_quote_key(
        self,
        source_id: str,
        quote_key: str,
    ) -> MarketEvent | None:
        """Return one exact source-local quote identity without scanning the mirror."""
        if type(source_id) is not str or not source_id or source_id.strip() != source_id:
            raise ValueError("source_id must be a non-empty trimmed string")
        if type(quote_key) is not str or not quote_key or quote_key.strip() != quote_key:
            raise ValueError("quote_key must be a non-empty trimmed string")
        with self._lock:
            event = self._latest.get((source_id, quote_key))
            return None if event is None else self._snapshot_event(event)

    def view_for_keys(
        self,
        keys: Iterable[tuple[str, str]],
        *,
        _causal_only: bool = False,
    ) -> MirrorSnapshot:
        """Return one coherent latest-event view for explicit source/quote keys.

        _causal_only is an internal authority fence for routing consumers that
        must not treat sealed generation-zero/audit-only state as live decision truth.
        Both raw and causal projections capture one mirror revision under the same
        lock and remain bounded to the requested identities.
        """
        normalized = self._quote_key_set(keys)
        if type(_causal_only) is not bool:
            raise TypeError("_causal_only must be a bool")
        with self._lock:
            revision = self._revision
            events = tuple(
                self._snapshot_event(self._latest[key])
                for key in sorted(normalized)
                if key in self._latest
                and (not _causal_only or key in self._decision_causal_keys)
            )
        return MirrorSnapshot(revision=revision, events=events)

    def active_view_for_keys(
        self,
        keys: Iterable[tuple[str, str]],
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> MirrorSnapshot:
        """Read decision-visible events for an explicit source/quote-key set.

        This is a focused read projection over the canonical latest-event map, not a
        second market-state authority. Incremental consumers can therefore avoid a
        whole-mirror snapshot when only bounded dirty quote identities changed.
        """
        normalized = self._quote_key_set(keys)
        boundary, age_limit = self._decision_boundary(as_of=as_of, max_age=max_age)
        with self._lock:
            revision = self._revision
            events = tuple(
                self._snapshot_event(self._latest[key])
                for key in sorted(normalized)
                if key in self._latest and key in self._decision_causal_keys
            )

        eligible = tuple(
            event
            for event in events
            if self._decision_visible_event(
                event,
                boundary=boundary,
                max_age=age_limit,
            )
        )
        return MirrorSnapshot(revision=revision, events=eligible)

    def snapshot(self) -> tuple[MarketEvent, ...]:
        """Return a deterministic, ownership-isolated snapshot by source and quote."""
        return self.view().events

    def get(
        self,
        source_id: str,
        event_id: str,
        market_id: str,
        selection_id: str,
        *,
        sport: str | None = None,
        exchange_side: str | None = None,
    ) -> MarketEvent | None:
        """Return one exact canonical quote without inferring identity dimensions.

        Omitting sport and exchange_side preserves the deployed legacy lookup byte-for-byte.
        Explicit callers must provide each canonical identity dimension they intend to
        address so same provider-local IDs cannot alias or be guessed across sports/sides.
        """
        quote_key = _quote_identity(
            event_id,
            market_id,
            selection_id,
            sport,
            exchange_side,
        )
        with self._lock:
            event = self._latest.get((source_id, quote_key))
            return None if event is None else self._snapshot_event(event)

    def active_snapshot(
        self,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> tuple[MarketEvent, ...]:
        """Return the full decision-eligible event tuple for compatibility."""
        return self.active_view(as_of=as_of, max_age=max_age).events

    @classmethod
    def _decision_view_from_proven_history(
        cls,
        events_with_generation: Iterable[tuple[MarketEvent, int]],
        *,
        boundary: datetime,
        max_age: timedelta,
        source_ids: frozenset[str] | None,
        sports: frozenset[str] | None,
        event_ids: frozenset[str] | None,
        market_ids: frozenset[str] | None,
        selection_ids: frozenset[str] | None,
    ) -> MirrorSnapshot:
        """Reconstruct latest causally available state from already-proven history."""

        mirror = cls()
        for event, append_generation in events_with_generation:
            if type(append_generation) is not int or append_generation < 0:
                raise ValueError(
                    "market history append generation must be a non-negative int"
                )
            if append_generation == 0:
                # Legacy baseline remains an audit/sequence fence only.
                mirror._apply_with_causal_authority(
                    event,
                    decision_causal=False,
                )
                continue
            if not cls._event_causally_available(event, boundary=boundary):
                # A future successor must not negatively erase the latest predecessor
                # that was actually available at this decision boundary.
                continue
            mirror._apply_with_causal_authority(
                event,
                decision_causal=True,
            )
        return mirror.active_view(
            as_of=boundary,
            max_age=max_age,
            source_ids=source_ids,
            sports=sports,
            event_ids=event_ids,
            market_ids=market_ids,
            selection_ids=selection_ids,
        )

    @classmethod
    def current_history_view_from_store(
        cls,
        store: SQLiteMarketStore,
        *,
        as_of: datetime,
        max_age: timedelta,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
    ) -> MirrorSnapshot:
        """Resolve current live as-of state from verified append history without a cutoff.

        This is an exceptional live fallback for a latest projection containing a
        causally-future successor. It verifies the existing product-issued append
        history but does not create a replay cutoff or a second durable authority.
        """

        if not isinstance(store, SQLiteMarketStore):
            raise TypeError("store must be a SQLiteMarketStore")
        boundary, age_limit = cls._decision_boundary(as_of=as_of, max_age=max_age)
        selected_sources = cls._selector(source_ids, name="source_ids")
        selected_sports = cls._selector(sports, name="sports")
        selected_events = cls._selector(event_ids, name="event_ids")
        selected_markets = cls._selector(market_ids, name="market_ids")
        selected_selections = cls._selector(selection_ids, name="selection_ids")
        return cls._decision_view_from_proven_history(
            store.events_with_append_generation(),
            boundary=boundary,
            max_age=age_limit,
            source_ids=selected_sources,
            sports=selected_sports,
            event_ids=selected_events,
            market_ids=selected_markets,
            selection_ids=selected_selections,
        )

    @classmethod
    def replay_view_from_store(
        cls,
        store: SQLiteMarketStore,
        *,
        as_of: datetime,
        max_age: timedelta,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
    ) -> MirrorSnapshot:
        """Reconstruct exactly the decision-visible mirror state at as_of.

        Replay never mutates canonical market history/current projection. The first
        read of an exact normalized as_of durably issues a causal cutoff by freezing
        the store's product-owned append generation; later appends therefore cannot
        rewrite that already-issued cutoff,
        even when they carry backdated local clocks. Pre-authority generation-zero
        migration rows remain sealed for tamper detection but are not admitted as
        causal decision history because their historical receipt chronology is
        unproven. Events whose local observation or ingestion/receipt instant is after
        as_of are still excluded. Malformed causal clocks fail closed. The
        reconstructed mirror then applies the same canonical
        status/freshness/selectors contract as a live active_view.
        """
        if not isinstance(store, SQLiteMarketStore):
            raise TypeError("store must be a SQLiteMarketStore")
        boundary, age_limit = cls._decision_boundary(as_of=as_of, max_age=max_age)

        # Validate and materialize every request selector before issuing the durable
        # forward-observed cutoff. A malformed selector must not permanently freeze
        # an otherwise valid decision instant, and one-shot iterables must not be
        # consumed once for validation and then silently disappear during filtering.
        selected_sources = cls._selector(source_ids, name="source_ids")
        selected_sports = cls._selector(sports, name="sports")
        selected_events = cls._selector(event_ids, name="event_ids")
        selected_markets = cls._selector(market_ids, name="market_ids")
        selected_selections = cls._selector(selection_ids, name="selection_ids")

        replay_events = store.replay_events_at_frozen_cutoff(
            as_of=boundary.isoformat(),
            _with_append_generation=True,
        )
        return cls._decision_view_from_proven_history(
            replay_events,
            boundary=boundary,
            max_age=age_limit,
            source_ids=selected_sources,
            sports=selected_sports,
            event_ids=selected_events,
            market_ids=selected_markets,
            selection_ids=selected_selections,
        )

    @classmethod
    def _from_proven_history(
        cls,
        events_with_generation: Iterable[tuple[MarketEvent, int]],
    ) -> "MarketMirror":
        """Restore one mirror from caller-owned, independently proven history."""

        mirror = cls()
        for item in events_with_generation:
            if type(item) is not tuple or len(item) != 2:
                raise TypeError(
                    "proven market history must contain "
                    "(MarketEvent, generation) tuples"
                )
            event, append_generation = item
            if not isinstance(event, MarketEvent):
                raise TypeError("proven market history must contain MarketEvent values")
            if type(append_generation) is not int or append_generation < 0:
                raise ValueError(
                    "proven market history append generation must be non-negative"
                )
            mirror._apply_with_causal_authority(
                event,
                decision_causal=append_generation > 0,
            )
        return mirror

    @classmethod
    def from_store(cls, store: SQLiteMarketStore) -> "MarketMirror":
        """Restore audit/order state without laundering legacy baseline into decisions.

        Every durable event remains in the mirror so restart preserves provider-local
        sequence fences and raw audit views. Only positive append generations are marked
        decision-causal; generation-zero migration rows stay visible through view() and
        snapshot() but are excluded from active decision views.
        """
        if not isinstance(store, SQLiteMarketStore):
            raise TypeError("store must be a SQLiteMarketStore")
        return cls._from_proven_history(
            store.events_with_append_generation()
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._latest)
