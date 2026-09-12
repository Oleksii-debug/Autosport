from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .paper import PaperBook


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    initial_bankroll: Decimal
    final_balance: Decimal
    settled_stake: Decimal
    net_profit: Decimal
    roi: Decimal
    won: int
    lost: int
    void: int


def evaluate(book: PaperBook) -> EvaluationSummary:
    settled = [ticket for ticket in book.tickets.values() if ticket.status.value != "open"]
    settled_stake = sum((ticket.stake for ticket in settled), Decimal("0"))
    profit = book.balance + book.committed_stake - book.initial_bankroll
    roi = (profit / settled_stake) if settled_stake else Decimal("0")
    return EvaluationSummary(
        initial_bankroll=book.initial_bankroll,
        final_balance=book.balance,
        settled_stake=settled_stake,
        net_profit=profit,
        roi=roi,
        won=sum(ticket.status.value == "won" for ticket in settled),
        lost=sum(ticket.status.value == "lost" for ticket in settled),
        void=sum(ticket.status.value == "void" for ticket in settled),
    )
