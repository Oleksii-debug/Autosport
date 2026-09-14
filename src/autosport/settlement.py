from __future__ import annotations

from dataclasses import dataclass, field

from .domain import TicketStatus
from .paper import PaperBook


VALID_OUTCOMES = {"win", "loss", "void"}


@dataclass(slots=True)
class SettlementEngine:
    """Version-1 deterministic settlement state. Strategy code never receives this state during replay."""

    outcomes: dict[str, str] = field(default_factory=dict)

    def record(self, quote_outcomes: dict[str, str]) -> None:
        for quote_key, outcome in quote_outcomes.items():
            if outcome not in VALID_OUTCOMES:
                raise ValueError(f"unsupported outcome: {outcome}")
            previous = self.outcomes.get(quote_key)
            if previous is not None and previous != outcome:
                raise ValueError(f"conflicting settlement for {quote_key}")
        self.outcomes.update(quote_outcomes)

    def settle_ready(self, book: PaperBook) -> list[str]:
        # Build and economically preflight the complete ready batch before
        # mutating the PaperBook. A later ticket can fail deterministic payout
        # validation even when an earlier ticket is valid; applying tickets as
        # they are discovered would leave a partially settled book.
        outcomes = dict(self.outcomes)
        plan: list[tuple[str, set[str], set[str]]] = []
        simulated_balance = book.balance

        for ticket in list(book.tickets.values()):
            if ticket.status is not TicketStatus.OPEN:
                continue
            states = [outcomes.get(leg.quote_key) for leg in ticket.legs]
            if "loss" not in states and any(state is None for state in states):
                continue
            winning = {
                leg.quote_key
                for leg in ticket.legs
                if outcomes.get(leg.quote_key) == "win"
            }
            voids = {
                leg.quote_key
                for leg in ticket.legs
                if outcomes.get(leg.quote_key) == "void"
            }
            _, _, simulated_balance = PaperBook._settlement_result(
                ticket,
                simulated_balance,
                winning,
                voids,
            )
            plan.append((ticket.ticket_id, winning, voids))

        settled: list[str] = []
        for ticket_id, winning, voids in plan:
            book.settle(ticket_id, winning, voids)
            settled.append(ticket_id)
        return settled
