from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .domain import PaperTicket, TicketLeg, TicketStatus, utc_now_iso


class PaperBook:
    """Virtual bankroll and auditable paper tickets. No real-money execution path exists."""

    def __init__(self, initial_bankroll: Decimal | str = Decimal("10000")) -> None:
        self.initial_bankroll = Decimal(str(initial_bankroll))
        self.balance = self.initial_bankroll
        self.tickets: dict[str, PaperTicket] = {}

    @property
    def committed_stake(self) -> Decimal:
        return sum((t.stake for t in self.tickets.values() if t.status is TicketStatus.OPEN), Decimal("0"))

    def open_ticket(
        self,
        legs: list[TicketLeg] | tuple[TicketLeg, ...],
        stake: Decimal | str,
        reason: str = "",
        placed_at: str | None = None,
    ) -> PaperTicket:
        amount = Decimal(str(stake))
        if amount <= 0:
            raise ValueError("stake must be positive")
        if amount > self.balance:
            raise ValueError("insufficient virtual bankroll")
        if not legs:
            raise ValueError("ticket requires at least one leg")
        if any(leg.locked_odds <= 1 for leg in legs):
            raise ValueError("decimal odds must be greater than 1")
        ticket = PaperTicket(
            ticket_id=str(uuid.uuid4()),
            stake=amount,
            legs=tuple(legs),
            placed_at=placed_at or utc_now_iso(),
            strategy_reason=reason,
        )
        self.balance -= amount
        self.tickets[ticket.ticket_id] = ticket
        return ticket

    def settle(
        self,
        ticket_id: str,
        winning_selection_ids: set[str],
        void_selection_ids: set[str] | None = None,
    ) -> PaperTicket:
        ticket = self.tickets[ticket_id]
        if ticket.status is not TicketStatus.OPEN:
            raise ValueError("ticket already settled")
        voids = void_selection_ids or set()
        effective_odds = Decimal("1")
        all_void = True
        for leg in ticket.legs:
            if leg.selection_id in voids:
                continue
            all_void = False
            if leg.selection_id not in winning_selection_ids:
                ticket.status = TicketStatus.LOST
                ticket.payout = Decimal("0")
                return ticket
            effective_odds *= leg.locked_odds
        ticket.payout = ticket.stake if all_void else ticket.stake * effective_odds
        ticket.status = TicketStatus.VOID if all_void else TicketStatus.WON
        self.balance += ticket.payout
        return ticket

    def save(self, path: str | Path) -> None:
        raw = {
            "initial_bankroll": str(self.initial_bankroll),
            "balance": str(self.balance),
            "tickets": [
                {
                    "ticket_id": t.ticket_id,
                    "stake": str(t.stake),
                    "placed_at": t.placed_at,
                    "status": t.status.value,
                    "payout": str(t.payout),
                    "strategy_reason": t.strategy_reason,
                    "legs": [
                        {
                            "event_id": leg.event_id,
                            "market_id": leg.market_id,
                            "selection_id": leg.selection_id,
                            "locked_odds": str(leg.locked_odds),
                        }
                        for leg in t.legs
                    ],
                }
                for t in self.tickets.values()
            ],
        }
        Path(path).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "PaperBook":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        book = cls(raw["initial_bankroll"])
        book.balance = Decimal(raw["balance"])
        for item in raw["tickets"]:
            ticket = PaperTicket(
                ticket_id=item["ticket_id"],
                stake=Decimal(item["stake"]),
                legs=tuple(
                    TicketLeg(
                        event_id=leg["event_id"],
                        market_id=leg["market_id"],
                        selection_id=leg["selection_id"],
                        locked_odds=Decimal(leg["locked_odds"]),
                    )
                    for leg in item["legs"]
                ),
                placed_at=item["placed_at"],
                status=TicketStatus(item["status"]),
                payout=Decimal(item["payout"]),
                strategy_reason=item.get("strategy_reason", ""),
            )
            book.tickets[ticket.ticket_id] = ticket
        return book
