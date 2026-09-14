from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal, DecimalException
from pathlib import Path

from .domain import PaperTicket, TicketLeg, TicketStatus, utc_now_iso


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"PaperBook snapshot contains duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"PaperBook snapshot contains non-finite JSON constant: {value}")


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
        ticket_placed_at = placed_at if placed_at is not None else utc_now_iso()
        self._require_canonical_text(ticket_placed_at, "placed_at")
        if not isinstance(reason, str):
            raise ValueError("PaperBook strategy_reason must be a string")
        ticket_legs = tuple(legs)
        if not ticket_legs:
            raise ValueError("ticket requires at least one leg")
        for leg in ticket_legs:
            self._validate_ticket_leg(leg)
        quote_keys = [leg.quote_key for leg in ticket_legs]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError("ticket contains duplicate quote_key leg")
        ticket = PaperTicket(
            ticket_id=str(uuid.uuid4()), stake=amount, legs=ticket_legs, placed_at=ticket_placed_at, strategy_reason=reason
        )
        self.balance -= amount
        self.tickets[ticket.ticket_id] = ticket
        return ticket

    def settle(self, ticket_id: str, winning_quote_keys: set[str], void_quote_keys: set[str] | None = None) -> PaperTicket:
        ticket = self.tickets[ticket_id]
        if ticket.status is not TicketStatus.OPEN:
            raise ValueError("ticket already settled")
        voids = void_quote_keys or set()
        effective_legs = tuple(leg for leg in ticket.legs if leg.quote_key not in voids)
        if any(leg.quote_key not in winning_quote_keys for leg in effective_legs):
            ticket.status = TicketStatus.LOST
            ticket.payout = Decimal("0")
            return ticket

        status = TicketStatus.VOID if not effective_legs else TicketStatus.WON
        try:
            effective_odds = Decimal("1")
            for leg in effective_legs:
                effective_odds *= leg.locked_odds
            payout = ticket.stake if status is TicketStatus.VOID else ticket.stake * effective_odds
            self._require_finite(payout, f"settlement payout for ticket {ticket.ticket_id}")
            new_balance = self.balance + payout
            self._require_finite(new_balance, f"balance after settling ticket {ticket.ticket_id}")
        except DecimalException as exc:
            raise ValueError("PaperBook settlement arithmetic is not representable") from exc

        ticket.payout = payout
        ticket.status = status
        self.balance = new_balance
        return ticket

    def save(self, path: str | Path) -> None:
        # PaperBook and PaperTicket are intentionally mutable during a paper run.
        # Revalidate the complete economic/identity state immediately before any
        # durable replacement so caller/agent mutation cannot persist a snapshot
        # that a trusted fresh load would reject.
        self._validate_loaded_state(self)
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
    def _require_finite(value: object, label: str) -> None:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"PaperBook snapshot contains non-finite {label}")

    @staticmethod
    def _require_canonical_text(
        value: object,
        label: str,
        *,
        forbid_quote_key_delimiter: bool = False,
    ) -> str:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(f"PaperBook {label} must be a non-empty trimmed string")
        if forbid_quote_key_delimiter and "|" in value:
            raise ValueError(f"PaperBook {label} must not contain quote-key delimiter '|'")
        return value

    @classmethod
    def _validate_ticket_leg(cls, leg: object, *, ticket_id: str | None = None) -> TicketLeg:
        if type(leg) is not TicketLeg:
            raise ValueError("PaperBook ticket legs must be canonical TicketLeg values")
        suffix = f" for ticket {ticket_id}" if ticket_id is not None else ""
        cls._require_canonical_text(
            leg.event_id,
            f"event_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_canonical_text(
            leg.market_id,
            f"market_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_canonical_text(
            leg.selection_id,
            f"selection_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_finite(leg.locked_odds, f"locked_odds{suffix}")
        if leg.locked_odds <= 1:
            raise ValueError("PaperBook snapshot decimal odds must be greater than 1")
        return leg

    @classmethod
    def _validate_loaded_state(cls, book: "PaperBook") -> None:
        cls._require_finite(book.initial_bankroll, "initial_bankroll")
        cls._require_finite(book.balance, "balance")
        if book.initial_bankroll <= 0:
            raise ValueError("PaperBook snapshot initial_bankroll must be positive")
        if book.balance < 0:
            raise ValueError("PaperBook snapshot balance cannot be negative")
        if type(book.tickets) is not dict:
            raise ValueError("PaperBook tickets must be a canonical ticket mapping")

        expected_balance = book.initial_bankroll
        for ticket_key, ticket in book.tickets.items():
            cls._require_canonical_text(ticket_key, "ticket mapping key")
            if type(ticket) is not PaperTicket:
                raise ValueError("PaperBook tickets must contain canonical PaperTicket values")
            cls._require_canonical_text(ticket.ticket_id, "ticket_id")
            if ticket_key != ticket.ticket_id:
                raise ValueError("PaperBook ticket mapping key must match ticket_id")
            cls._require_canonical_text(ticket.placed_at, f"placed_at for ticket {ticket.ticket_id}")
            if not isinstance(ticket.strategy_reason, str):
                raise ValueError("PaperBook snapshot strategy_reason must be a string")
            if type(ticket.status) is not TicketStatus:
                raise ValueError("PaperBook snapshot ticket status must be canonical TicketStatus")
            cls._require_finite(ticket.stake, f"stake for ticket {ticket.ticket_id}")
            cls._require_finite(ticket.payout, f"payout for ticket {ticket.ticket_id}")
            if ticket.stake <= 0:
                raise ValueError("PaperBook snapshot ticket stake must be positive")
            if ticket.payout < 0:
                raise ValueError("PaperBook snapshot ticket payout cannot be negative")
            if type(ticket.legs) is not tuple or not ticket.legs:
                raise ValueError("PaperBook snapshot ticket requires a canonical non-empty leg tuple")
            for leg in ticket.legs:
                cls._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
            quote_keys = [leg.quote_key for leg in ticket.legs]
            if len(quote_keys) != len(set(quote_keys)):
                raise ValueError("PaperBook snapshot ticket contains duplicate quote_key leg")

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
            parse_constant=_reject_nonfinite_json_constant,
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
