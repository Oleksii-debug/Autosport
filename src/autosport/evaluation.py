from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, Underflow, localcontext

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
        # Inexact means non-zero information was discarded. Rounded alone can be
        # representation-only (for example 1E+28 + 0) and must not reject an
        # otherwise exact canonical book.
        context.traps[Inexact] = True
    context.clear_flags()
    return context


def _validate_evaluation_economic_precision(
    book: PaperBook,
    tickets: dict[str, PaperTicket],
) -> None:
    """Reject mutable book states whose stake/payout effects disappear at canonical precision."""

    with localcontext(_evaluation_decimal_context()) as context:
        expected_balance = book.initial_bankroll
        for ticket in tickets.values():
            context.clear_flags()
            after_stake = expected_balance - ticket.stake
            # Stake is an explicit paper-economic input. If debiting it discards
            # non-zero information, the book can contain exposure that never
            # reached the cash balance, so durable evaluation must fail closed.
            if context.flags[Inexact]:
                raise ValueError("PaperBook stake debit loses Decimal precision")
            expected_balance = after_stake

            if ticket.status is not TicketStatus.OPEN:
                before_payout = expected_balance
                context.clear_flags()
                after_payout = expected_balance + ticket.payout
                # Canonical settlement can legitimately round a long parlay payout
                # while adding it to a much larger balance. Preserve that existing
                # product behavior, but never allow a non-zero settled payout to be
                # swallowed completely by precision.
                if ticket.payout != 0 and after_payout == before_payout:
                    raise ValueError("PaperBook settled payout loses all Decimal effect")
                expected_balance = after_payout

        if expected_balance != book.balance:
            raise ValueError(
                "PaperBook balance is inconsistent with canonical ticket arithmetic"
            )


def evaluate(book: PaperBook) -> EvaluationSummary:
    """Publish paper-evaluation metrics only from a valid canonical book state."""

    try:
        tickets = book.tickets
        if not isinstance(tickets, dict):
            raise TypeError("PaperBook tickets must be a dict")
        for ticket_key, ticket in tickets.items():
            if not isinstance(ticket_key, str) or not ticket_key:
                raise TypeError("PaperBook contains a noncanonical ticket mapping key")
            if not isinstance(ticket, PaperTicket) or not isinstance(ticket.status, TicketStatus):
                raise TypeError("PaperBook contains a noncanonical ticket/status")
            if (
                not isinstance(ticket.ticket_id, str)
                or not ticket.ticket_id
                or ticket_key != ticket.ticket_id
            ):
                raise TypeError("PaperBook contains a noncanonical ticket identity")

        # Evaluation becomes durable run evidence. Re-prove both the canonical
        # PaperBook invariant and the precision-sensitive cash-flow boundary before
        # calculating metrics. This is isolated from caller Decimal traps/flags.
        _validate_evaluation_economic_precision(book, tickets)
        with localcontext(_evaluation_decimal_context()):
            PaperBook._validate_loaded_state(book)
            initial_bankroll = book.initial_bankroll
            final_balance = book.balance
            settled = tuple(
                ticket for ticket in tickets.values() if ticket.status is not TicketStatus.OPEN
            )

        # Aggregate/profit evidence must not silently lose non-zero information.
        # Rounded without Inexact remains acceptable because it changes only the
        # Decimal representation, not the numeric value.
        with localcontext(_evaluation_decimal_context(exact=True)):
            committed_stake = book.committed_stake
            settled_stake = sum((ticket.stake for ticket in settled), Decimal("0"))
            net_profit = final_balance + committed_stake - initial_bankroll

        # ROI can be a legitimate non-terminating ratio, so canonical rounding is
        # allowed only at this presentation/evaluation ratio step.
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
