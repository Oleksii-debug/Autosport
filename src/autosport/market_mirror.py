from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .domain import MarketEvent


class MirrorUpdate(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class MirrorApplyResult:
    status: MirrorUpdate
    quote_key: str
    previous_sequence: int | None
    current_sequence: int


class MarketMirror:
    """Deterministic in-memory mirror for normalized market quotes.

    The mirror is deliberately presentation- and provider-adapter-neutral: it stores
    canonical ``MarketEvent`` values and enforces source-local ordering. Persistence,
    provider polling and portfolio/economic decisions remain outside this boundary.
    """

    _INACTIVE_STATUSES = frozenset({"suspended", "closed", "unavailable"})

    def __init__(self) -> None:
        self._latest: dict[tuple[str, str], MarketEvent] = {}

    @staticmethod
    def _key(event: MarketEvent) -> tuple[str, str]:
        # Include source identity so two providers using the same local IDs cannot
        # overwrite one another's state.
        return (event.source_id, event.quote_key)

    def apply(self, event: MarketEvent) -> MirrorApplyResult:
        """Apply one event iff it advances source-local sequence state.

        A repeated identical sequence is idempotent.  A lower sequence is stale and
        ignored.  Reusing an existing sequence for different content is a conflict
        and fails closed rather than silently replacing canonical evidence.
        """
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")

        key = self._key(event)
        previous = self._latest.get(key)
        if previous is None:
            self._latest[key] = event
            return MirrorApplyResult(
                MirrorUpdate.APPLIED,
                event.quote_key,
                None,
                event.sequence,
            )

        if event.sequence < previous.sequence:
            return MirrorApplyResult(
                MirrorUpdate.STALE,
                event.quote_key,
                previous.sequence,
                previous.sequence,
            )

        if event.sequence == previous.sequence:
            if event == previous:
                return MirrorApplyResult(
                    MirrorUpdate.DUPLICATE,
                    event.quote_key,
                    previous.sequence,
                    previous.sequence,
                )
            raise ValueError(
                "conflicting MarketEvent payload reused an existing source-local sequence"
            )

        self._latest[key] = event
        return MirrorApplyResult(
            MirrorUpdate.APPLIED,
            event.quote_key,
            previous.sequence,
            event.sequence,
        )

    def snapshot(self) -> tuple[MarketEvent, ...]:
        """Return a deterministic snapshot ordered by source and quote identity."""
        return tuple(
            event
            for _, event in sorted(self._latest.items(), key=lambda item: item[0])
        )

    def get(
        self,
        source_id: str,
        event_id: str,
        market_id: str,
        selection_id: str,
    ) -> MarketEvent | None:
        """Return the latest source-specific quote, if present."""
        return self._latest.get(
            (source_id, f"{event_id}|{market_id}|{selection_id}")
        )

    def active_snapshot(self) -> tuple[MarketEvent, ...]:
        """Return deterministic snapshot entries currently eligible for decisions.

        The mirror preserves suspended/closed/unavailable observations for auditability
        but does not expose them as active decision inputs through this helper.
        """
        return tuple(
            event
            for event in self.snapshot()
            if event.status not in self._INACTIVE_STATUSES
        )

    def __len__(self) -> int:
        return len(self._latest)
