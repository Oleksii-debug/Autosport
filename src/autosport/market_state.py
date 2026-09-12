from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .domain import EventKind, MarketEvent, MarketKey


@dataclass(frozen=True, slots=True)
class SelectionState:
    key: MarketKey
    decimal_odds: Decimal | None = None
    value: str | None = None
    last_event_id: str | None = None
    last_sequence: int = -1


class MarketState:
    """Idempotent current-state projection from immutable market events."""

    def __init__(self) -> None:
        self._selections: dict[MarketKey, SelectionState] = {}
        self._seen_event_ids: set[str] = set()

    def apply(self, event: MarketEvent) -> bool:
        if event.event_id in self._seen_event_ids:
            return False
        current = self._selections.get(event.key)
        if current is not None and event.sequence < current.last_sequence:
            raise ValueError("stale sequence would rewind current market state")

        odds = current.decimal_odds if current else None
        value = current.value if current else None
        if event.kind is EventKind.ODDS:
            odds = event.decimal_odds
        else:
            value = event.value

        self._selections[event.key] = SelectionState(
            key=event.key,
            decimal_odds=odds,
            value=value,
            last_event_id=event.event_id,
            last_sequence=event.sequence,
        )
        self._seen_event_ids.add(event.event_id)
        return True

    def get(self, key: MarketKey) -> SelectionState | None:
        return self._selections.get(key)

    def snapshot(self) -> tuple[SelectionState, ...]:
        return tuple(self._selections[key] for key in sorted(self._selections, key=lambda k: (k.provider, k.match_id, k.market_id, k.selection_id)))
