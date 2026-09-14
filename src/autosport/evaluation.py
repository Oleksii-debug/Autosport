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


def _validated_evaluation_state(
    book: PaperBook,
) -> tuple[
    Decimal,
    Decimal,
    Decimal,
    Decimal,
    Decimal,
    tuple[PaperTicket, ...],
    Decimal,
]:
    """Validate the mutable paper ledger and derive finite evaluation economics.

    Evaluation is durable product evidence, so it must not trust a mutable
    PaperBook merely because the object originated from a previously validated
    snapshot. Reuse the canonical PaperBook invariant immediately before
    publishing metrics, and isolate Decimal behavior from the caller context.
    Ledger aggregates and profit must be exact at the canonical precision;
    non-terminating ROI division may use normal Decimal rounding.
    """

    try:
        tickets = book.tickets
        if not isinstance(tickets, dict):
            raise TypeError("PaperBook tickets must be a dict")
        for ticket in tickets.values():
            if not isinstance(ticket, PaperTicket) or not isinstance(ticket.status, TicketStatus):
                raise TypeError("PaperBook contains a noncanonical ticket/status")

        with localcontext(_evaluation_decimal_context(exact=True)):
            PaperBook._validate_loaded_state(book)
            initial_bankroll = book.initial_bankroll
            final_balance = book.balance
            committed_stake = book.committed_stake
            settled = tuple(
                ticket for ticket in tickets.values() if ticket.status is not TicketStatus.OPEN
            )
            settled_stake = sum((ticket.stake for ticket in settled), Decimal("0"))
            net_profit = final_balance + committed_stake - initial_bankroll

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

    return (
        initial_bankroll,
        final_balance,
        committed_stake,
        settled_stake,
        net_profit,
        settled,
        roi,
    )


def evaluate(book: PaperBook) -> EvaluationSummary:
    (
        initial_bankroll,
        final_balance,
        committed_stake,
        settled_stake,
        net_profit,
        settled,
        roi,
    ) = _validated_evaluation_state(book)
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
