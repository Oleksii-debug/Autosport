from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .domain import MarketEvent
from .storage import SQLiteMarketStore


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
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def apply(self, event: MarketEvent) -> MirrorApplyResult:
        """Apply one event iff it advances source-local sequence state.

        A repeated identical sequence is idempotent. A lower sequence is stale and
        ignored. Reusing an existing sequence for different content is a conflict
        and fails closed rather than silently replacing canonical evidence.
        """
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")

        key = self._key(event)
        previous = self._latest.get(key)
        if previous is None:
            self._latest[key] = self._snapshot_event(event)
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
        return MirrorApplyResult(
            MirrorUpdate.APPLIED,
            event.source_id,
            event.quote_key,
            previous.sequence,
            event.sequence,
        )

    def snapshot(self) -> tuple[MarketEvent, ...]:
        """Return a deterministic, ownership-isolated snapshot by source and quote."""
        return tuple(
            self._snapshot_event(event)
            for _, event in sorted(self._latest.items(), key=lambda item: item[0])
        )

    def get(
        self,
        source_id: str,
        event_id: str,
        market_id: str,
        selection_id: str,
    ) -> MarketEvent | None:
        """Return an isolated copy of the latest source-specific quote, if present."""
        event = self._latest.get(
            (source_id, f"{event_id}|{market_id}|{selection_id}")
        )
        return None if event is None else self._snapshot_event(event)

    def active_snapshot(
        self,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> tuple[MarketEvent, ...]:
        """Return deterministic, active and fresh entries eligible for decisions.

        Only explicitly recognized decision-eligible statuses are returned. Unknown,
        inactive, future, over-age or malformed observations fail closed and remain
        visible only through the full audit snapshot. ``source_ts`` is the preferred
        freshness clock because it represents provider time; ``observed_ts`` is used
        only when source time is unavailable.
        """
        if not isinstance(as_of, datetime):
            raise TypeError("as_of must be a datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(max_age, timedelta):
            raise TypeError("max_age must be a timedelta")
        if max_age < timedelta(0):
            raise ValueError("max_age must be non-negative")

        boundary = as_of.astimezone(timezone.utc)
        eligible: list[MarketEvent] = []
        for event in self.snapshot():
            if event.status not in self._DECISION_ELIGIBLE_STATUSES:
                continue
            timestamp = self._utc_timestamp(event.source_ts or event.observed_ts)
            if timestamp is None:
                continue
            age = boundary - timestamp
            if timedelta(0) <= age <= max_age:
                eligible.append(event)
        return tuple(eligible)

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
        return len(self._latest)
