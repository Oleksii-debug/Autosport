from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .domain import TicketStatus
from .paper import PaperBook


VALID_OUTCOMES = {"win", "loss", "void"}


@dataclass(slots=True, init=False)
class SettlementEngine:
    """Version-1 deterministic settlement state. Strategy code never receives this state during replay."""

    _outcomes: dict[str, str] = field(default_factory=dict, repr=False)

    def __init__(self, outcomes: Mapping[str, str] | None = None) -> None:
        self._outcomes = {}
        if outcomes is not None:
            self.record(outcomes)

    @property
    def outcomes(self) -> Mapping[str, str]:
        """Read-only live view of validated settlement outcomes."""
        return MappingProxyType(self._outcomes)

    def record(self, quote_outcomes: Mapping[str, str]) -> None:
        # Snapshot caller-owned input before validation so the exact values that
        # pass the batch gate are the values committed below.
        candidate = dict(quote_outcomes)
        for quote_key, outcome in candidate.items():
            if outcome not in VALID_OUTCOMES:
                raise ValueError(f"unsupported outcome: {outcome}")
            previous = self._outcomes.get(quote_key)
            if previous is not None and previous != outcome:
                raise ValueError(f"conflicting settlement for {quote_key}")
        self._outcomes.update(candidate)

    def settle_ready(self, book: PaperBook) -> list[str]:
        settled: list[str] = []
        for ticket in list(book.tickets.values()):
            if ticket.status is not TicketStatus.OPEN:
                continue
            states = [self._outcomes.get(leg.quote_key) for leg in ticket.legs]
            if "loss" in states:
                winning = {leg.quote_key for leg in ticket.legs if self._outcomes.get(leg.quote_key) == "win"}
                voids = {leg.quote_key for leg in ticket.legs if self._outcomes.get(leg.quote_key) == "void"}
                book.settle(ticket.ticket_id, winning, voids)
                settled.append(ticket.ticket_id)
                continue
            if any(state is None for state in states):
                continue
            winning = {leg.quote_key for leg in ticket.legs if self._outcomes.get(leg.quote_key) == "win"}
            voids = {leg.quote_key for leg in ticket.legs if self._outcomes.get(leg.quote_key) == "void"}
            book.settle(ticket.ticket_id, winning, voids)
            settled.append(ticket.ticket_id)
        return settled
