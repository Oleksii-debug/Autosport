from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

from .domain import PaperTicket, TicketLeg, TicketStatus, utc_now_iso


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"PaperBook snapshot contains duplicate JSON key: {key}")
        payload[key] = value
    return payload


class PaperBook:
    """Virtual bankroll and auditable paper tickets. No real-money execution path exists."""

    def __init__(self, initial_bankroll: Decimal | str = Decimal("10000")) -> None:
        initial = Decimal(str(initial_bankroll))
        self._require_finite(initial, "initial_bankroll")
        if initial <= 0:
            raise ValueError("initial virtual bankroll must be positive")
        self.initial_bankroll = initial
        self.balance = self.initial_bankroll
        self.tickets: dict[str, PaperTicket] = {}

    @property
    def committed_stake(self) -> Decimal:
        return sum((t.stake for t in self.tickets.values() if t.status is TicketStatus.OPEN), Decimal("0"))

    def open_ticket(self, legs, stake, reason: str = "", placed_at: str | None = None) -> PaperTicket:
        amount = Decimal(str(stake))
        self._require_finite(amount, "stake")
        if amount <= 0:
            raise ValueError("stake must be positive")
        if amount > self.balance:
            raise ValueError("insufficient virtual bankroll")
        ticket_legs = tuple(legs)
        if not ticket_legs:
            raise ValueError("ticket requires at least one leg")
        quote_keys = [leg.quote_key for leg in ticket_legs]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError("ticket contains duplicate quote_key leg")
        for leg in ticket_legs:
            self._require_finite(leg.locked_odds, "locked_odds")
            if leg.locked_odds <= 1:
                raise ValueError("decimal odds must be greater than 1")
        ticket = PaperTicket(
            ticket_id=str(uuid.uuid4()), stake=amount, legs=ticket_legs, placed_at=placed_at or utc_now_iso(), strategy_reason=reason
        )
        self.balance -= amount
        self.tickets[ticket.ticket_id] = ticket
        return ticket

    def settle(self, ticket_id: str, winning_quote_keys: set[str], void_quote_keys: set[str] | None = None) -> PaperTicket:
        ticket = self.tickets[ticket_id]
        if ticket.status is not TicketStatus.OPEN:
            raise ValueError("ticket already settled")
        voids = void_quote_keys or set()
        effective_odds = Decimal("1")
        all_void = True
        for leg in ticket.legs:
            if leg.quote_key in voids:
                continue
            all_void = False
            if leg.quote_key not in winning_quote_keys:
                ticket.status = TicketStatus.LOST
                ticket.payout = Decimal("0")
                return ticket
            effective_odds *= leg.locked_odds
        ticket.payout = ticket.stake if all_void else ticket.stake * effective_odds
        ticket.status = TicketStatus.VOID if all_void else TicketStatus.WON
        self.balance += ticket.payout
        return ticket

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
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
                        {"event_id": leg.event_id, "market_id": leg.market_id, "selection_id": leg.selection_id, "locked_odds": str(leg.locked_odds)}
                        for leg in t.legs
                    ],
                }
                for t in self.tickets.values()
            ],
        }
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)

    @staticmethod
    def _require_finite(value: Decimal, label: str) -> None:
        if not value.is_finite():
            raise ValueError(f"PaperBook snapshot contains non-finite {label}")

    @classmethod
    def _validate_loaded_state(cls, book: "PaperBook") -> None:
        cls._require_finite(book.initial_bankroll, "initial_bankroll")
        cls._require_finite(book.balance, "balance")
        if book.initial_bankroll <= 0:
            raise ValueError("PaperBook snapshot initial_bankroll must be positive")
        if book.balance < 0:
            raise ValueError("PaperBook snapshot balance cannot be negative")

        expected_balance = book.initial_bankroll
        for ticket in book.tickets.values():
            cls._require_finite(ticket.stake, f"stake for ticket {ticket.ticket_id}")
            cls._require_finite(ticket.payout, f"payout for ticket {ticket.ticket_id}")
            if ticket.stake <= 0:
                raise ValueError("PaperBook snapshot ticket stake must be positive")
            if ticket.payout < 0:
                raise ValueError("PaperBook snapshot ticket payout cannot be negative")
            if not ticket.legs:
                raise ValueError("PaperBook snapshot ticket requires at least one leg")
            quote_keys = [leg.quote_key for leg in ticket.legs]
            if len(quote_keys) != len(set(quote_keys)):
                raise ValueError("PaperBook snapshot ticket contains duplicate quote_key leg")
            for leg in ticket.legs:
                cls._require_finite(leg.locked_odds, f"locked_odds for ticket {ticket.ticket_id}")
                if leg.locked_odds <= 1:
                    raise ValueError("PaperBook snapshot decimal odds must be greater than 1")

            if ticket.status in {TicketStatus.OPEN, TicketStatus.LOST} and ticket.payout != 0:
                raise ValueError("PaperBook snapshot open/lost ticket payout must be zero")
            if ticket.status is TicketStatus.VOID and ticket.payout != ticket.stake:
                raise ValueError("PaperBook snapshot void ticket payout must equal stake")
            if ticket.status is TicketStatus.WON and ticket.payout <= ticket.stake:
                raise ValueError("PaperBook snapshot won ticket payout must exceed stake")

            expected_balance -= ticket.stake
            if ticket.status is not TicketStatus.OPEN:
                expected_balance += ticket.payout

        if expected_balance != book.balance:
            raise ValueError(
                "PaperBook snapshot balance is inconsistent with ticket stakes and settled payouts"
            )

    @classmethod
    def load(cls, path: str | Path) -> "PaperBook":
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        book = cls(raw["initial_bankroll"])
        book.balance = Decimal(raw["balance"])
        seen_ticket_ids: set[str] = set()
        for item in raw["tickets"]:
            ticket_id = item["ticket_id"]
            if not isinstance(ticket_id, str) or not ticket_id:
                raise ValueError("PaperBook snapshot ticket_id must be a non-empty string")
            if ticket_id in seen_ticket_ids:
                raise ValueError("PaperBook snapshot contains duplicate ticket_id")
            seen_ticket_ids.add(ticket_id)
            ticket = PaperTicket(
                ticket_id=ticket_id,
                stake=Decimal(item["stake"]),
                legs=tuple(
                    TicketLeg(leg["event_id"], leg["market_id"], leg["selection_id"], Decimal(leg["locked_odds"])) for leg in item["legs"]
                ),
                placed_at=item["placed_at"],
                status=TicketStatus(item["status"]),
                payout=Decimal(item["payout"]),
                strategy_reason=item.get("strategy_reason", ""),
            )
            book.tickets[ticket.ticket_id] = ticket
        cls._validate_loaded_state(book)
        return book