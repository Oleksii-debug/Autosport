from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock

from .domain import MarketEvent, _quote_identity
from .storage import SQLiteMarketStore, _timezone_aware_instant


class MirrorUpdate(str, Enum):
    APPLIED = "applied"
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
    def _decision_boundary(*, as_of: datetime, max_age: timedelta) -> tuple[datetime, timedelta]:
        if type(as_of) is not datetime:
            raise TypeError("as_of must be an exact datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if type(max_age) is not timedelta:
            raise TypeError("max_age must be an exact timedelta")
        if max_age < timedelta(0):
            raise ValueError("max_age must be non-negative")
        return as_of.astimezone(timezone.utc), max_age

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
        source_time = cls._utc_timestamp(event.source_ts or event.observed_ts)
        observed_time = cls._utc_timestamp(event.observed_ts)
        ingest_time = cls._utc_timestamp(event.ingest_ts)
        if (
            source_time is None
            or observed_time is None
            or ingest_time is None
            or source_time > boundary
            or observed_time > boundary
            or ingest_time > boundary
        ):
            return False
        age = boundary - source_time
        return timedelta(0) <= age <= max_age

    def apply(self, event: MarketEvent) -> MirrorApplyResult:
        """Apply one event iff it advances source-local sequence state.

        A repeated identical sequence is idempotent. A lower sequence is stale and
        ignored. Reusing an existing sequence for different content is a conflict
        and fails closed rather than silently replacing canonical evidence. Material
        updates are serialized with readers and advance one mirror-wide revision.
        """
        if type(event) is not MarketEvent:
            raise TypeError("event must be an exact MarketEvent")

        key = self._key(event)
        with self._lock:
            previous = self._latest.get(key)
            if previous is None:
                self._latest[key] = self._snapshot_event(event)
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

            self._latest[key] = self._snapshot_event(event)
            self._revision += 1
            return MirrorApplyResult(
                MirrorUpdate.APPLIED,
                event.source_id,
                event.quote_key,
                previous.sequence,
                event.sequence,
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
        if type(store) is not SQLiteMarketStore:
            raise TypeError("store must be an exact SQLiteMarketStore")
        if type(event) is not MarketEvent:
            raise TypeError("event must be an exact MarketEvent")

        prior = self.event_for_quote_key(event.source_id, event.quote_key)
        accepted = store.append_batch_accepted((event,))
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
        if prior is not None and prior.sequence >= event.sequence:
            return self.apply(event)

        # Storage intentionally treats a retry of one source-local sequence as the
        # same provider observation even when local receipt clocks changed. On a
        # fresh/incomplete mirror, applying the caller's retry object would expose
        # local timestamps that are not the durable canonical history. Re-read the
        # independently trusted persisted event before mutating live state.
        canonical = next(
            (
                persisted
                for persisted in store.events(event.event_id)
                if persisted.dedupe_key == event.dedupe_key
            ),
            None,
        )
        if canonical is None:
            raise RuntimeError(
                "duplicate market event disappeared from canonical history"
            )
        return self.apply(canonical)

    def view(
        self,
        *,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
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

        with self._lock:
            revision = self._revision
            events = tuple(
                self._snapshot_event(event)
                for _, event in sorted(self._latest.items(), key=lambda item: item[0])
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
        captured = self.view(
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

        boundary, age_limit = self._decision_boundary(as_of=as_of, max_age=max_age)
        with self._lock:
            revision = self._revision
            events = tuple(
                self._snapshot_event(self._latest[key])
                for key in sorted(normalized)
                if key in self._latest
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
        even when they carry backdated local clocks. Events whose local observation or
        ingestion/receipt instant is after as_of are still excluded. Malformed causal
        clocks fail closed. The reconstructed mirror then applies the same canonical
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

        mirror = cls()
        replay_events = store.replay_events_at_frozen_cutoff(
            as_of=boundary.isoformat()
        )
        for event in replay_events:
            observed = cls._utc_timestamp(event.observed_ts)
            ingested = cls._utc_timestamp(event.ingest_ts)
            if observed is None or ingested is None:
                continue
            if observed <= boundary and ingested <= boundary:
                mirror.apply(event)
        return mirror.active_view(
            as_of=boundary,
            max_age=age_limit,
            source_ids=selected_sources,
            sports=selected_sports,
            event_ids=selected_events,
            market_ids=selected_markets,
            selection_ids=selected_selections,
        )

    @classmethod
    def from_store(cls, store: SQLiteMarketStore) -> "MarketMirror":
        """Restore latest source-specific mirror state from authoritative history.

        The canonical store remains the only writer/owner of durable market history.
        Replaying ``store.events()`` reconstructs source-local sequence protection after
        restart without letting this mirror mutate the store's shared current projection.
        """
        if not isinstance(store, SQLiteMarketStore):
            raise TypeError("store must be a SQLiteMarketStore")
        mirror = cls()
        for event in store.events():
            mirror.apply(event)
        return mirror

    def __len__(self) -> int:
        with self._lock:
            return len(self._latest)
