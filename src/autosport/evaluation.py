from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)

from .domain import TicketStatus
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

    # Context() inherits every unspecified policy field from mutable process-global
    # decimal.DefaultContext. Pin the complete policy so unrelated library/caller
    # changes cannot alter durable evaluation results or failure behavior.
    context = Context(
        prec=28,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[],
    )
    context.traps[InvalidOperation] = True
    context.traps[DivisionByZero] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    if exact:
        # Inexact means non-zero information was discarded. Rounded alone can be
        # representation-only (for example 1E+28 + 0) and must not reject an
        # otherwise exact canonical book.
        context.traps[Inexact] = True
    context.clear_flags()
    return context


def evaluate(book: PaperBook) -> EvaluationSummary:
    """Publish paper-evaluation metrics only from a valid canonical book state."""

    try:
        # Evaluation becomes durable run evidence. Since #322, PaperBook owns the
        # canonical economic reachability proof, including exact open/settle
        # chronology in its lifecycle witness and its private Decimal policy.
        # Do not reconstruct a second chronology from ticket insertion order:
        # Decimal addition is non-associative at the canonical 28-digit precision.
        with localcontext(_evaluation_decimal_context()):
            PaperBook._validate_loaded_state(book)
            tickets = book.tickets
            initial_bankroll = book.initial_bankroll
            final_balance = book.balance
            settled = tuple(
                ticket
                for ticket in tickets.values()
                if ticket.status is not TicketStatus.OPEN
            )

        # Aggregate/profit evidence must not silently lose non-zero information.
        # Rounded without Inexact remains acceptable because it changes only the
        # Decimal representation, not the numeric value.
        with localcontext(_evaluation_decimal_context(exact=True)):
            committed_stake = book.committed_stake
            settled_stake = sum(
                (ticket.stake for ticket in settled),
                Decimal("0"),
            )
            net_profit = final_balance + committed_stake - initial_bankroll

        # ROI can be a legitimate non-terminating ratio, so canonical rounding is
        # allowed only at this presentation/evaluation ratio step.
        with localcontext(_evaluation_decimal_context()):
            roi = (
                net_profit / settled_stake
                if settled_stake
                else Decimal("0")
            )
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
    if any(
        not isinstance(value, Decimal) or not value.is_finite()
        for value in values
    ):
        raise ValueError(_INVALID_EVALUATION_STATE)
    if (
        initial_bankroll <= 0
        or final_balance < 0
        or committed_stake < 0
        or settled_stake < 0
    ):
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
