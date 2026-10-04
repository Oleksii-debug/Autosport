from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock

from .domain import MarketEvent, _quote_identity
from .storage import SQLiteMarketStore


def _require_market_event(
    event: object,
    *,
    _event_type: type[MarketEvent] = MarketEvent,
) -> MarketEvent:
    if type(event) is not _event_type:
        raise TypeError("event must be an exact MarketEvent")
    return event


def _require_market_store(
    store: object,
    *,
    exact: bool,
    error_message: str,
    _store_type: type[SQLiteMarketStore] = SQLiteMarketStore,
) -> SQLiteMarketStore:
    valid = type(store) is _store_type if exact else isinstance(store, _store_type)
    if not valid:
        raise TypeError(error_message)
    return store


def _trusted_live_events(
    store: SQLiteMarketStore,
    *,
    _read=SQLiteMarketStore.trusted_live_events,
) -> list[MarketEvent]:
    return _read(store)


def _trusted_live_current_by_source(
    store: SQLiteMarketStore,
    *,
    _read=SQLiteMarketStore.trusted_live_current_by_source,
) -> dict[tuple[str, str], MarketEvent]:
    return _read(store)


class MarketMirrorRevisionChanged(RuntimeError):
    """The canonical mirror advanced past a decision's captured revision."""


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


def _trusted_mirror_event_key(
    event: MarketEvent,
    *,
    _quote_key=MarketEvent.quote_key.fget,
) -> tuple[str, str]:
    if type(event) is not MarketEvent:
        raise TypeError("trusted mirror event must be an exact MarketEvent")
    if _quote_key is None:
        raise RuntimeError("canonical quote_key descriptor is unavailable")
    return (event.source_id, _quote_key(event))


def _trusted_mirror_event_snapshot(
    event: MarketEvent,
    *,
    _event_type: type[MarketEvent] = MarketEvent,
    _to_dict=MarketEvent.to_dict,
    _from_dict=MarketEvent.from_dict,
) -> MarketEvent:
    if type(event) is not _event_type:
        raise TypeError("trusted mirror event must be an exact MarketEvent")
    snapshot = _from_dict(_to_dict(event))
    if type(snapshot) is not _event_type:
        raise TypeError("trusted mirror snapshot lost canonical MarketEvent authority")
    return snapshot


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
        self._publication_revision_guard: int | None = None

    @property
    def revision(self) -> int:
        """Return the exact current mirror revision without copying market state."""
        with self._lock:
            return self._revision

    @contextmanager
    def hold_revision(self, expected_revision: int) -> Iterator[None]:
        """Linearize a short publication step against one captured mirror revision."""

        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        with self._lock:
            if self._publication_revision_guard is not None:
                raise MarketMirrorRevisionChanged(
                    "market mirror revision guard is already active"
                )
            if self._revision != expected_revision:
                raise MarketMirrorRevisionChanged(
                    "market mirror revision changed before decision publication"
                )
            self._publication_revision_guard = expected_revision
            try:
                yield
                if self._revision != expected_revision:
                    raise MarketMirrorRevisionChanged(
                        "market mirror revision changed during decision publication"
                    )
            finally:
                self._publication_revision_guard = None

    @staticmethod
    def _key(
        event: MarketEvent,
        *,
        _quote_key=MarketEvent.quote_key.fget,
    ) -> tuple[str, str]:
        # Include source identity so two providers using the same local IDs cannot
        # overwrite one another's state. Seal the canonical quote-key descriptor so
        # an in-process rebind cannot redirect trusted state after persistence.
        if _quote_key is None:
            raise RuntimeError("canonical quote_key descriptor is unavailable")
        return (event.source_id, _quote_key(event))

    @staticmethod
    def _snapshot_event(
        event: MarketEvent,
        *,
        _event_type: type[MarketEvent] = MarketEvent,
        _to_dict=MarketEvent.to_dict,
        _from_dict=MarketEvent.from_dict,
    ) -> MarketEvent:
        """Own an independent canonical value snapshot, including nested metadata."""
        if type(event) is not _event_type:
            raise TypeError("event must be an exact MarketEvent")
        snapshot = _from_dict(_to_dict(event))
        if type(snapshot) is not _event_type:
            raise TypeError("market mirror snapshot lost canonical MarketEvent authority")
        return snapshot

    @staticmethod
    def _same_sequence_payload(
        left: MarketEvent,
        right: MarketEvent,
        *,
        _to_dict=MarketEvent.to_dict,
    ) -> bool:
        """Compare provider payload truth while ignoring local receipt clocks.

        ``observed_ts`` and ``ingest_ts`` are local process timestamps. A retry or
        replay of one provider sequence may legitimately receive different local
        timestamps, and canonical storage uses the same identity rule. Provider time
        (``source_ts``) and every economic/provider/provenance field remain part of the
        conflict check.
        """
        left_payload = _to_dict(left)
        right_payload = _to_dict(right)
        for local_clock in ("observed_ts", "ingest_ts"):
            left_payload.pop(local_clock, None)
            right_payload.pop(local_clock, None)
        return left_payload == right_payload

    @staticmethod
    def _utc_timestamp(value: str) -> datetime | None:
        """Parse one provider/observation timestamp, failing closed on bad input."""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
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
        event = _require_market_event(event)

        key = self._key(event)
        with self._lock:
            if self._publication_revision_guard is not None:
                raise MarketMirrorRevisionChanged(
                    "market mirror mutation is blocked during decision publication"
                )
            previous = self._latest.get(key)
            if previous is None:
                self._latest[key] = self._snapshot_event(event)
                self._revision += 1
                return MirrorApplyResult(
                    MirrorUpdate.APPLIED,
                    event.source_id,
                    key[1],
                    None,
                    event.sequence,
                )

            if event.sequence < previous.sequence:
                return MirrorApplyResult(
                    MirrorUpdate.STALE,
                    event.source_id,
                    key[1],
                    previous.sequence,
                    previous.sequence,
                )

            if event.sequence == previous.sequence:
                if self._same_sequence_payload(event, previous):
                    return MirrorApplyResult(
                        MirrorUpdate.DUPLICATE,
                        event.source_id,
                        key[1],
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
                key[1],
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
        canonical_store = _require_market_store(
            store,
            exact=False,
            error_message="store must be a SQLiteMarketStore",
        )
        event = _require_market_event(event)

        with self._lock:
            if self._publication_revision_guard is not None:
                raise MarketMirrorRevisionChanged(
                    "market mirror persistence is blocked during decision publication"
                )
            # Keep durable append and live revision advance in one mirror critical
            # section. A decision publication guard can therefore linearize before
            # the append or after the applied revision, never between them.
            canonical_store.append(event)
            return self.apply(event)

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
        require_live_receipt_authority: bool = False,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
        _require_store=_require_market_store,
        _trusted_reader=_trusted_live_events,
    ) -> MirrorSnapshot:
        """Reconstruct exactly the decision-visible mirror state at as_of.

        Replay is read-only over canonical append-only history. Live-decision recovery
        may additionally require durable product-owned receipt authority, preventing
        legacy/import rows from promoting caller-supplied ingest_ts into a live clock.
        Events whose local observation or ingestion/receipt instant is after as_of are never applied,
        even when their provider timestamp is older, so later-received evidence cannot
        leak into an earlier decision. Malformed causal clocks fail closed. The
        reconstructed mirror then applies the same canonical status/freshness/selectors
        contract as a live active_view.
        """
        if type(require_live_receipt_authority) is not bool:
            raise TypeError("require_live_receipt_authority must be bool")
        if require_live_receipt_authority and cls is not __class__:
            raise TypeError("trusted live replay requires an exact MarketMirror")
        canonical_store = _require_store(
            store,
            exact=require_live_receipt_authority,
            error_message=(
                "trusted live replay requires an exact SQLiteMarketStore"
                if require_live_receipt_authority
                else "store must be a SQLiteMarketStore"
            ),
        )
        boundary, age_limit = cls._decision_boundary(as_of=as_of, max_age=max_age)
        mirror = cls()
        history = (
            _trusted_reader(canonical_store)
            if require_live_receipt_authority
            else canonical_store.events()
        )
        for event in history:
            observed = cls._utc_timestamp(event.observed_ts)
            ingested = cls._utc_timestamp(event.ingest_ts)
            if observed is None or ingested is None:
                continue
            if observed <= boundary and ingested <= boundary:
                mirror.apply(event)
        return mirror.active_view(
            as_of=boundary,
            max_age=age_limit,
            source_ids=source_ids,
            sports=sports,
            event_ids=event_ids,
            market_ids=market_ids,
            selection_ids=selection_ids,
        )

    @classmethod
    def from_store(cls, store: SQLiteMarketStore) -> "MarketMirror":
        """Restore latest source-specific state from the canonical current projection.

        SQLiteMarketStore rebuilds and validates current_quotes from append-only history
        when it opens. The mirror needs only the latest provider sequence for each
        source/quote key to preserve stale-update protection after restart, so replaying
        every historical observation here would add unbounded startup cost without
        adding authority.
        """
        canonical_store = _require_market_store(
            store,
            exact=False,
            error_message="store must be a SQLiteMarketStore",
        )
        mirror = cls()
        current = canonical_store.current_by_source()
        for key in sorted(current):
            mirror.apply(current[key])
        return mirror

    @classmethod
    def from_live_store(
        cls,
        store: SQLiteMarketStore,
        *,
        _require_store=_require_market_store,
        _trusted_current=_trusted_live_current_by_source,
        _object_new=object.__new__,
        _lock_factory=RLock,
        _event_key=_trusted_mirror_event_key,
        _snapshot_event=_trusted_mirror_event_snapshot,
    ) -> "MarketMirror":
        """Restore only rows with durable product-owned live receipt authority.

        Legacy, replay and imported rows remain canonical market history but cannot
        self-promote their caller-supplied ingest_ts into a live receipt clock on
        restart. The receipt side table is intentionally prospective: rows written
        before that authority existed stay absent from this live projection.
        """
        if cls is not __class__:
            raise TypeError("live store bootstrap requires an exact MarketMirror")
        canonical_store = _require_store(
            store,
            exact=True,
            error_message="live store must be an exact SQLiteMarketStore",
        )
        current = _trusted_current(canonical_store)
        mirror = _object_new(cls)
        mirror._latest = {}
        mirror._revision = 0
        mirror._lock = _lock_factory()
        mirror._publication_revision_guard = None
        for expected_key in sorted(current):
            event = _snapshot_event(current[expected_key])
            canonical_key = _event_key(event)
            if expected_key != canonical_key:
                raise ValueError("trusted live current key does not match MarketEvent identity")
            mirror._latest[canonical_key] = event
            mirror._revision += 1
        return mirror

    def __len__(self) -> int:
        with self._lock:
            return len(self._latest)

def _seal_trusted_recovery_call_surfaces() -> None:
    """Expose trusted recovery contracts without caller-replaceable canonical hooks."""

    replay_impl = MarketMirror.__dict__["replay_view_from_store"].__func__
    live_bootstrap_impl = MarketMirror.__dict__["from_live_store"].__func__

    def replay_view_from_store(
        cls,
        store: SQLiteMarketStore,
        *,
        as_of: datetime,
        max_age: timedelta,
        require_live_receipt_authority: bool = False,
        source_ids: str | Iterable[str] | None = None,
        sports: str | Iterable[str] | None = None,
        event_ids: str | Iterable[str] | None = None,
        market_ids: str | Iterable[str] | None = None,
        selection_ids: str | Iterable[str] | None = None,
    ) -> MirrorSnapshot:
        return replay_impl(
            cls,
            store,
            as_of=as_of,
            max_age=max_age,
            require_live_receipt_authority=require_live_receipt_authority,
            source_ids=source_ids,
            sports=sports,
            event_ids=event_ids,
            market_ids=market_ids,
            selection_ids=selection_ids,
        )

    def from_live_store(cls, store: SQLiteMarketStore) -> "MarketMirror":
        return live_bootstrap_impl(cls, store)

    MarketMirror.replay_view_from_store = classmethod(replay_view_from_store)
    MarketMirror.from_live_store = classmethod(from_live_store)


_seal_trusted_recovery_call_surfaces()
del _seal_trusted_recovery_call_surfaces
