from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
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


def _evaluation_decimal_context() -> Context:
    """Return the deterministic context used for the intentionally rounded ROI."""

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
    context.clear_flags()
    return context


def _exact_decimal_sum(values: tuple[Decimal, ...]) -> Decimal:
    """Sum canonical finite Decimals exactly with bounded coefficient scaling."""

    policy = _evaluation_decimal_context()
    components: list[tuple[int, int]] = []
    minimum_exponent: int | None = None

    for value in values:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(_INVALID_EVALUATION_STATE)
        parts = value.as_tuple()
        original_exponent = parts.exponent
        if not isinstance(original_exponent, int):
            raise ValueError(_INVALID_EVALUATION_STATE)
        if not any(parts.digits):
            # Zero carries no scale information for an exact mathematical sum and
            # must not widen the exponent span of non-zero operands.
            continue

        significant_end = len(parts.digits)
        while significant_end > 1 and parts.digits[significant_end - 1] == 0:
            significant_end -= 1
        significant_digits = parts.digits[:significant_end]
        normalized_exponent = original_exponent + (
            len(parts.digits) - significant_end
        )
        adjusted_exponent = normalized_exponent + len(significant_digits) - 1

        # Bound the exact accumulator by the canonical Decimal exponent envelope,
        # not by working precision. The normalized/adjusted exponent pair jointly
        # bounds both the significant coefficient length and every later 10**gap
        # expansion, while still allowing canonical PaperBook values with more
        # than 28 significant digits when no lossy Decimal arithmetic is required.
        if (
            normalized_exponent < policy.Etiny()
            or adjusted_exponent > policy.Emax
        ):
            raise ValueError(_INVALID_EVALUATION_STATE)

        # Representation-only trailing zeroes do not carry mathematical scale for
        # exact addition. Strip them before integer accumulation to minimize the
        # bounded coefficient and exponent work without changing the value.
        component_digits = significant_digits
        component_exponent = normalized_exponent

        coefficient = 0
        for digit in component_digits:
            coefficient = coefficient * 10 + digit
        if parts.sign:
            coefficient = -coefficient
        components.append((coefficient, component_exponent))
        minimum_exponent = (
            component_exponent
            if minimum_exponent is None
            else min(minimum_exponent, component_exponent)
        )

    if minimum_exponent is None:
        return Decimal("0")

    total = 0
    for coefficient, exponent in components:
        # Source validation above bounds this gap to the canonical Decimal
        # representable envelope rather than attacker-controlled raw exponents.
        total += coefficient * (10 ** (exponent - minimum_exponent))

    if total == 0:
        return Decimal("0")

    result_digits = Decimal(abs(total)).as_tuple().digits
    removable = min(max(0, -minimum_exponent), len(result_digits) - 1)
    stripped = 0
    while stripped < removable and result_digits[-1 - stripped] == 0:
        stripped += 1
    if stripped:
        result_digits = result_digits[:-stripped]
    canonical_exponent = minimum_exponent + stripped

    return Decimal((int(total < 0), result_digits, canonical_exponent))


def evaluate(book: PaperBook) -> EvaluationSummary:
    """Publish paper-evaluation metrics only from a valid canonical book state."""

    try:
        # Evaluation becomes durable run evidence. Since #322, PaperBook owns the
        # canonical economic reachability proof, including exact open/settle
        # chronology in its lifecycle witness and its private Decimal policy.
        # Do not reconstruct a second chronology from ticket insertion order.
        with localcontext(_evaluation_decimal_context()):
            PaperBook._validate_loaded_state(book)
            tickets = tuple(book.tickets.values())
            initial_bankroll = book.initial_bankroll
            final_balance = book.balance

        settled = tuple(
            ticket
            for ticket in tickets
            if ticket.status is not TicketStatus.OPEN
        )
        open_stakes = tuple(
            ticket.stake
            for ticket in tickets
            if ticket.status is TicketStatus.OPEN
        )
        committed_stake = _exact_decimal_sum(open_stakes)
        settled_stake = _exact_decimal_sum(
            tuple(ticket.stake for ticket in settled)
        )
        # Feed canonical raw operands rather than the possibly >28-digit aggregate
        # back into the source-operand validator. This keeps exact 29+ digit open
        # stake totals valid while net profit remains mathematically exact.
        net_profit = _exact_decimal_sum(
            (
                final_balance,
                *open_stakes,
                initial_bankroll.copy_negate(),
            )
        )

        # ROI can be a legitimate non-terminating ratio, so canonical 28-digit
        # rounding is intentional only at this presentation/evaluation ratio step.
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
