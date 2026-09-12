from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable


class TicketStatus(StrEnum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    VOID = "void"


@dataclass(frozen=True, slots=True)
class TicketLeg:
    match_id: str
    market_id: str
    selection_id: str
    decimal_odds: Decimal

    def __post_init__(self) -> None:
        if self.decimal_odds <= Decimal("1"):
            raise ValueError("decimal_odds must be > 1")


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket_id: str
    stake: Decimal
    legs: tuple[TicketLeg, ...]
    status: TicketStatus = TicketStatus.OPEN
    payout: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.ticket_id:
            raise ValueError("ticket_id must be non-empty")
        if self.stake <= 0:
            raise ValueError("stake must be positive")
        if not self.legs:
            raise ValueError("ticket must contain at least one leg")

    @property
    def quoted_odds(self) -> Decimal:
        product = Decimal("1")
        for leg in self.legs:
            product *= leg.decimal_odds
        return product

    @property
    def potential_payout(self) -> Decimal:
        return self.stake * self.quoted_odds


class PaperBook:
    """Exact-decimal paper bankroll with idempotent settlement."""

    def __init__(self, initial_bankroll: Decimal, currency: str = "UAH") -> None:
        if initial_bankroll <= 0:
            raise ValueError("initial_bankroll must be positive")
        self.initial_bankroll = initial_bankroll
        self.currency = currency
        self._cash = initial_bankroll
        self._tickets: dict[str, Ticket] = {}

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def tickets(self) -> tuple[Ticket, ...]:
        return tuple(self._tickets.values())

    @property
    def open_stake(self) -> Decimal:
        return sum(
            (ticket.stake for ticket in self._tickets.values() if ticket.status is TicketStatus.OPEN),
            Decimal("0"),
        )

    @property
    def equity_if_open_stakes_returned(self) -> Decimal:
        return self._cash + self.open_stake

    def place(self, ticket_id: str, stake: Decimal, legs: Iterable[TicketLeg]) -> Ticket:
        if ticket_id in self._tickets:
            raise ValueError("duplicate ticket_id")
        ticket = Ticket(ticket_id=ticket_id, stake=stake, legs=tuple(legs))
        if stake > self._cash:
            raise ValueError("insufficient paper bankroll")
        self._cash -= stake
        self._tickets[ticket_id] = ticket
        return ticket

    def settle(self, ticket_id: str, status: TicketStatus) -> Ticket:
        if status is TicketStatus.OPEN:
            raise ValueError("settlement status must be terminal")
        current = self._tickets[ticket_id]
        if current.status is not TicketStatus.OPEN:
            if current.status is status:
                return current
            raise ValueError("ticket already settled with different status")

        if status is TicketStatus.WON:
            payout = current.potential_payout
        elif status is TicketStatus.VOID:
            payout = current.stake
        else:
            payout = Decimal("0")

        settled = Ticket(
            ticket_id=current.ticket_id,
            stake=current.stake,
            legs=current.legs,
            status=status,
            payout=payout,
        )
        self._tickets[ticket_id] = settled
        self._cash += payout
        return settled
