from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Iterator

from .domain import MarketEvent


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    clock: datetime
    released_count: int
    remaining_count: int


class CausalReplay:
    """Deterministic causal event release.

    The full future stream is held privately by the engine. Consumers receive only
    events whose observed_at is <= the replay clock. No API exposes the unreleased
    tail, which prevents an ordinary strategy from inspecting future outcomes.
    """

    def __init__(self, events: Iterable[MarketEvent]) -> None:
        ordered = sorted(events, key=lambda event: event.sort_key)
        ids = [event.event_id for event in ordered]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate event_id in replay input")
        self._events: tuple[MarketEvent, ...] = tuple(ordered)
        self._index = 0
        self._clock: datetime | None = None

    @property
    def clock(self) -> datetime | None:
        return self._clock

    def advance_to(self, clock: datetime) -> tuple[MarketEvent, ...]:
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("replay clock must be timezone-aware")
        if self._clock is not None and clock < self._clock:
            raise ValueError("causal replay clock cannot move backwards")
        self._clock = clock
        released: list[MarketEvent] = []
        while self._index < len(self._events):
            event = self._events[self._index]
            if event.observed_at > clock:
                break
            released.append(event)
            self._index += 1
        return tuple(released)

    def step(self) -> tuple[MarketEvent, ...]:
        """Advance directly to the next event timestamp (event-driven replay)."""
        if self._index >= len(self._events):
            return ()
        return self.advance_to(self._events[self._index].observed_at)

    def run(self) -> Iterator[tuple[MarketEvent, ...]]:
        while self._index < len(self._events):
            yield self.step()

    def snapshot(self) -> ReplaySnapshot | None:
        if self._clock is None:
            return None
        return ReplaySnapshot(
            clock=self._clock,
            released_count=self._index,
            remaining_count=len(self._events) - self._index,
        )

    @property
    def finished(self) -> bool:
        return self._index >= len(self._events)
