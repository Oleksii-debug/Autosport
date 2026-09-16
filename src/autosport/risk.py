from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)

from .domain import PaperTicket, TicketLeg, TicketStatus
from .economic_goal import EconomicGoalContract
from .paper import PaperBook


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ProposedTicketRiskContext:
    """Non-persistent facts about a proposed paper ticket at the risk boundary.

    ``legs`` reuses the canonical TicketLeg identity rather than copying or
    inferring event/market/selection identifiers.  The optional identity/window
    fields are carried exactly as supplied; this seam does not bind them to
    PaperBook and does not make them durable authority.

    Follow-on policies may consume this typed seam for event/market
    concentration and parlay-leg ceilings.  Provider/sport deny-lists,
    quote-freshness/slippage/data-quality limits, and session/day windows need
    their own proven upstream facts before they can become executable limits.
    """

    legs: tuple[TicketLeg, ...]
    bankroll_id: str | None = None
    currency: str | None = None
    measurement_session_id: str | None = None
    measurement_day: str | None = None
    source_id: str | None = None
    quote_observed_ts: str | None = None
    quote_source_ts: str | None = None
    data_quality_evidence: str | None = None
    slippage: Decimal | None = None

    @property
    def parlay_leg_count(self) -> int:
        return len(self.legs)


@dataclass(frozen=True, slots=True)
class PaperRiskPolicy:
    """Paper-lab guardrails. Limits are explicit and deterministic, never inferred by an LLM.

    ``economic_goal`` can only tighten the locally provable executable limits in
    this policy: per-ticket stake, aggregate committed capital, concurrent open
    paper positions, and the owner emergency stop.  Other EconomicGoalContract
    dimensions deliberately remain outside this boundary until the typed
    proposal context or another canonical runtime seam proves the needed facts.
    """

    max_ticket_fraction: Decimal = Decimal("0.02")
    max_committed_fraction: Decimal = Decimal("0.20")
    minimum_cash_reserve_fraction: Decimal = Decimal("0.20")
    economic_goal: EconomicGoalContract | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "max_ticket_fraction",
            "max_committed_fraction",
            "minimum_cash_reserve_fraction",
        ):
            raw_value = getattr(self, field_name)
            try:
                value = Decimal(str(raw_value))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{field_name} must be a finite decimal") from exc
            if not value.is_finite():
                raise ValueError(f"{field_name} must be a finite decimal")
            if value < 0 or value > 1:
                raise ValueError(f"{field_name} must be between 0 and 1 inclusive")
            object.__setattr__(self, field_name, value)
        if self.economic_goal is not None and not isinstance(
            self.economic_goal, EconomicGoalContract
        ):
            raise TypeError("economic_goal must be an EconomicGoalContract or None")

    @staticmethod
    def _decimal_context() -> Context:
        context = Context(prec=28, Emin=-999999, Emax=999999)
        context.traps[Inexact] = True
        context.traps[InvalidOperation] = True
        context.traps[Overflow] = True
        context.traps[Underflow] = True
        context.clear_flags()
        return context

    @staticmethod
    def _exact_positive_sum(values: tuple[Decimal, ...]) -> Decimal:
        """Sum canonical non-negative exposure exactly, independent of caller context."""
        if not values:
            return Decimal("0")
        min_exponent: int | None = None
        max_adjusted: int | None = None
        nonzero_count = 0
        for value in values:
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(
                    "committed exposure must contain non-negative finite Decimal values"
                )
            if value.is_zero():
                continue
            decimal_tuple = value.as_tuple()
            exponent = int(decimal_tuple.exponent)
            adjusted = exponent + len(decimal_tuple.digits) - 1
            min_exponent = (
                exponent if min_exponent is None else min(min_exponent, exponent)
            )
            max_adjusted = (
                adjusted if max_adjusted is None else max(max_adjusted, adjusted)
            )
            nonzero_count += 1
        if nonzero_count == 0:
            return Decimal("0")
        assert min_exponent is not None and max_adjusted is not None
        required_precision = (
            max_adjusted - min_exponent + 1 + len(str(nonzero_count))
        )
        context = Context(
            prec=max(1, required_precision),
            rounding=ROUND_HALF_EVEN,
            Emin=-999999,
            Emax=999999,
        )
        context.traps[Inexact] = True
        context.traps[InvalidOperation] = True
        context.traps[Overflow] = True
        context.traps[Underflow] = True
        context.clear_flags()
        with localcontext(context):
            return sum(values, Decimal("0"))

    @classmethod
    def _book_state(
        cls, book: PaperBook
    ) -> tuple[Decimal, Decimal, Decimal, int] | None:
        try:
            tickets = book.tickets
            if not isinstance(tickets, dict):
                return None
            for ticket_key, ticket in tickets.items():
                if not isinstance(ticket, PaperTicket) or not isinstance(
                    ticket.status, TicketStatus
                ):
                    return None
                if (
                    not isinstance(ticket.ticket_id, str)
                    or not ticket.ticket_id
                    or ticket_key != ticket.ticket_id
                ):
                    return None

            # PaperBook owns canonical lifecycle/settlement semantics. Do not impose the
            # risk context's Inexact trap on that validator.
            PaperBook._validate_loaded_state(book)
            initial_bankroll = book.initial_bankroll
            balance = book.balance
            open_tickets = tuple(
                ticket
                for ticket in tickets.values()
                if ticket.status is TicketStatus.OPEN
            )
            committed_stake = cls._exact_positive_sum(
                tuple(ticket.stake for ticket in open_tickets)
            )
            open_position_count = len(open_tickets)
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return None

        values = (initial_bankroll, balance, committed_stake)
        if any(
            not isinstance(value, Decimal) or not value.is_finite()
            for value in values
        ):
            return None
        if initial_bankroll <= 0 or balance < 0 or committed_stake < 0:
            return None
        return initial_bankroll, balance, committed_stake, open_position_count

    @staticmethod
    def _proposal_context_is_valid(
        proposal: ProposedTicketRiskContext,
    ) -> bool:
        if not isinstance(proposal, ProposedTicketRiskContext):
            return False
        if type(proposal.legs) is not tuple or not proposal.legs:
            return False

        quote_keys: set[str] = set()
        for leg in proposal.legs:
            try:
                # Reuse PaperBook's canonical TicketLeg validator so proposal
                # identity cannot become a looser parallel definition.
                PaperBook._validate_ticket_leg(leg)
            except (AttributeError, TypeError, ValueError):
                return False
            if leg.quote_key in quote_keys:
                return False
            quote_keys.add(leg.quote_key)

        for field_name in (
            "bankroll_id",
            "currency",
            "measurement_session_id",
            "measurement_day",
            "source_id",
            "quote_observed_ts",
            "quote_source_ts",
            "data_quality_evidence",
        ):
            value = getattr(proposal, field_name)
            if value is not None and (
                type(value) is not str or not value or value != value.strip()
            ):
                return False

        if proposal.slippage is not None and (
            not isinstance(proposal.slippage, Decimal)
            or not proposal.slippage.is_finite()
        ):
            return False
        return True

    def _effective_fraction_limits(self) -> tuple[Decimal, Decimal]:
        goal = self.economic_goal
        if goal is None:
            return self.max_ticket_fraction, self.max_committed_fraction
        return (
            min(self.max_ticket_fraction, goal.max_stake_fraction),
            min(self.max_committed_fraction, goal.max_capital_at_risk_fraction),
        )

    def _derived_risk_values(
        self,
        initial_bankroll: Decimal,
        balance: Decimal,
        committed_stake: Decimal,
        amount: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None:
        try:
            ticket_fraction, committed_fraction = self._effective_fraction_limits()
            # Protective caps/reserve remain fail-closed on any limit-relaxing rounding.
            with localcontext(self._decimal_context()):
                ticket_limit = initial_bankroll * ticket_fraction
                committed_limit = initial_bankroll * committed_fraction
                remaining_balance = balance - amount
                reserve_limit = (
                    initial_bankroll * self.minimum_cash_reserve_fraction
                )
            # Exposure itself can legitimately require more than 28 significant digits even
            # when every PaperBook debit was canonical, so aggregate it exactly.
            aggregate_committed = self._exact_positive_sum(
                (committed_stake, amount)
            )
        except (ArithmeticError, TypeError, ValueError):
            return None

        values = (
            ticket_limit,
            aggregate_committed,
            committed_limit,
            remaining_balance,
            reserve_limit,
        )
        if any(not value.is_finite() for value in values):
            return None
        return values

    def evaluate(
        self,
        book: PaperBook,
        stake: Decimal | str,
        proposal: ProposedTicketRiskContext | None = None,
    ) -> RiskDecision:
        try:
            amount = Decimal(str(stake))
        except (InvalidOperation, ValueError):
            return RiskDecision(False, "stake must be a finite decimal")
        if not amount.is_finite():
            return RiskDecision(False, "stake must be a finite decimal")
        if amount <= 0:
            return RiskDecision(False, "stake must be positive")

        if proposal is not None and not self._proposal_context_is_valid(proposal):
            return RiskDecision(False, "proposed-ticket risk context is invalid")

        state = self._book_state(book)
        if state is None:
            return RiskDecision(False, "virtual bankroll state is invalid")
        initial_bankroll, balance, committed_stake, open_position_count = state

        goal = self.economic_goal
        if goal is not None:
            if goal.emergency_stop:
                return RiskDecision(False, "economic goal emergency stop is active")
            if (
                goal.max_stake_amount is not None
                and amount > goal.max_stake_amount
            ):
                return RiskDecision(
                    False,
                    "ticket exceeds economic goal absolute stake limit",
                )
            if open_position_count >= goal.max_concurrent_positions:
                return RiskDecision(
                    False,
                    "economic goal concurrent position limit exceeded",
                )

        derived = self._derived_risk_values(
            initial_bankroll, balance, committed_stake, amount
        )
        if derived is None:
            return RiskDecision(False, "virtual bankroll state is invalid")
        (
            ticket_limit,
            aggregate_committed,
            committed_limit,
            remaining_balance,
            reserve_limit,
        ) = derived

        if amount > ticket_limit:
            return RiskDecision(
                False, "ticket exceeds configured bankroll fraction"
            )
        if aggregate_committed > committed_limit:
            return RiskDecision(False, "aggregate committed stake limit exceeded")
        if remaining_balance < reserve_limit:
            return RiskDecision(
                False, "minimum virtual cash reserve would be violated"
            )
        return RiskDecision(True, "allowed")
