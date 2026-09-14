from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Underflow,
    localcontext,
)

from .domain import PaperTicket, TicketStatus
from .paper import PaperBook


_INVALID_EVALUATION_STATE = "virtual bankroll state is invalid for evaluation"


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    initial_bankroll: Decimal
    final_balance: Decimal
    committed_stake: Decimal
    settled_stake: Decimal
    net_profit: Decimal
    roi: Decimal
    won: int
    lost: int
    void: int


def _evaluation_decimal_context(*, exact: bool = False) -> Context:
    """Return the deterministic context used for durable evaluation economics."""

    context = Context(prec=28, Emin=-999999, Emax=999999)
    context.traps[InvalidOperation] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    if exact:
        context.traps[Inexact] = True
        context.traps[Rounded] = True
    context.clear_flags()
    return context


def evaluate(book: PaperBook) -> EvaluationSummary:
    """Publish paper-evaluation metrics only from a valid canonical book state."""

    try:
        tickets = book.tickets
        if not isinstance(tickets, dict):
            raise TypeError("PaperBook tickets must be a dict")
        for ticket in tickets.values():
            if not isinstance(ticket, PaperTicket) or not isinstance(ticket.status, TicketStatus):
                raise TypeError("PaperBook contains a noncanonical ticket/status")

        # Re-prove the mutable PaperBook invariant immediately before evaluation,
        # independent of caller Decimal traps/flags. The validator intentionally
        # keeps canonical 28-digit rounding because PaperBook settlement/balance
        # mutation uses that arithmetic and valid persisted books can contain its
        # rounded results.
        with localcontext(_evaluation_decimal_context()):
            PaperBook._validate_loaded_state(book)
            initial_bankroll = book.initial_bankroll
            final_balance = book.balance
            settled = tuple(
                ticket for ticket in tickets.values() if ticket.status is not TicketStatus.OPEN
            )

        # Newly published aggregate/profit evidence must not acquire additional
        # silent rounding. If the canonical precision cannot represent these
        # derived values exactly, fail closed instead of publishing path-dependent
        # economic truth.
        with localcontext(_evaluation_decimal_context(exact=True)):
            committed_stake = book.committed_stake
            settled_stake = sum((ticket.stake for ticket in settled), Decimal("0"))
            net_profit = final_balance + committed_stake - initial_bankroll

        # ROI is a ratio and can legitimately be non-terminating (for example
        # -1 / 3), so canonical Decimal rounding is permitted only at this step.
        with localcontext(_evaluation_decimal_context()):
            roi = (net_profit / settled_stake) if settled_stake else Decimal("0")
    except (ArithmeticError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError(_INVALID_EVALUATION_STATE) from exc

    values = (
        initial_bankroll,
        final_balance,
        committed_stake,
        settled_stake,
        net_profit,
        roi,
    )
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise ValueError(_INVALID_EVALUATION_STATE)
    if initial_bankroll <= 0 or final_balance < 0 or committed_stake < 0 or settled_stake < 0:
        raise ValueError(_INVALID_EVALUATION_STATE)

    return EvaluationSummary(
        initial_bankroll=initial_bankroll,
        final_balance=final_balance,
        committed_stake=committed_stake,
        settled_stake=settled_stake,
        net_profit=net_profit,
        roi=roi,
        won=sum(ticket.status is TicketStatus.WON for ticket in settled),
        lost=sum(ticket.status is TicketStatus.LOST for ticket in settled),
        void=sum(ticket.status is TicketStatus.VOID for ticket in settled),
    )
