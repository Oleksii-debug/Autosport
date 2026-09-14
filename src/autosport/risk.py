from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, Underflow, localcontext

from .domain import PaperTicket, TicketStatus
from .paper import PaperBook


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PaperRiskPolicy:
    """Paper-lab guardrails. Limits are explicit and deterministic, never inferred by an LLM."""

    max_ticket_fraction: Decimal = Decimal("0.02")
    max_committed_fraction: Decimal = Decimal("0.20")
    minimum_cash_reserve_fraction: Decimal = Decimal("0.20")

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

    @staticmethod
    def _decimal_context() -> Context:
        """Return the deterministic context used for risk-state validation and limit arithmetic."""

        context = Context(prec=28, Emin=-999999, Emax=999999)
        # Risk boundaries must never silently relax because Decimal rounded an otherwise-finite
        # calculation. Inexact covers non-zero discarded digits; exact normalization that only
        # discards insignificant zeroes remains acceptable.
        context.traps[Inexact] = True
        context.traps[InvalidOperation] = True
        context.traps[Overflow] = True
        context.traps[Underflow] = True
        context.clear_flags()
        return context

    @classmethod
    def _book_state(cls, book: PaperBook) -> tuple[Decimal, Decimal, Decimal] | None:
        """Return validated finite economic state, or None when risk cannot be evaluated safely."""

        try:
            tickets = book.tickets
            if not isinstance(tickets, dict):
                return None
            for ticket_key, ticket in tickets.items():
                if not isinstance(ticket, PaperTicket) or not isinstance(ticket.status, TicketStatus):
                    return None
                # PaperBook.load() establishes this identity invariant before validating
                # economics. Recheck it for mutable in-memory state so an aliased ticket cannot
                # be counted twice under a fabricated mapping key and corresponding balance.
                if (
                    not isinstance(ticket.ticket_id, str)
                    or not ticket.ticket_id
                    or ticket_key != ticket.ticket_id
                ):
                    return None

            # Reuse the canonical durable-book invariant rather than trusting a derived aggregate.
            # Running it in our own Decimal context keeps this risk boundary independent of caller
            # traps/flags while proving ticket economics and the cross-field balance equation.
            with localcontext(cls._decimal_context()):
                PaperBook._validate_loaded_state(book)
                initial_bankroll = book.initial_bankroll
                balance = book.balance
                committed_stake = book.committed_stake
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return None

        values = (initial_bankroll, balance, committed_stake)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            return None
        if initial_bankroll <= 0 or balance < 0 or committed_stake < 0:
            return None
        return values

    def _derived_risk_values(
        self,
        initial_bankroll: Decimal,
        balance: Decimal,
        committed_stake: Decimal,
        amount: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None:
        """Calculate limit values without leaking Decimal context/range or precision failures."""

        try:
            with localcontext(self._decimal_context()):
                ticket_limit = initial_bankroll * self.max_ticket_fraction
                aggregate_committed = committed_stake + amount
                committed_limit = initial_bankroll * self.max_committed_fraction
                remaining_balance = balance - amount
                reserve_limit = initial_bankroll * self.minimum_cash_reserve_fraction
        except ArithmeticError:
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

    def evaluate(self, book: PaperBook, stake: Decimal | str) -> RiskDecision:
        try:
            amount = Decimal(str(stake))
        except (InvalidOperation, ValueError):
            return RiskDecision(False, "stake must be a finite decimal")
        if not amount.is_finite():
            return RiskDecision(False, "stake must be a finite decimal")
        if amount <= 0:
            return RiskDecision(False, "stake must be positive")

        state = self._book_state(book)
        if state is None:
            return RiskDecision(False, "virtual bankroll state is invalid")
        initial_bankroll, balance, committed_stake = state

        derived = self._derived_risk_values(initial_bankroll, balance, committed_stake, amount)
        if derived is None:
            return RiskDecision(False, "virtual bankroll state is invalid")
        ticket_limit, aggregate_committed, committed_limit, remaining_balance, reserve_limit = derived

        if amount > ticket_limit:
            return RiskDecision(False, "ticket exceeds configured bankroll fraction")
        if aggregate_committed > committed_limit:
            return RiskDecision(False, "aggregate committed stake limit exceeded")
        if remaining_balance < reserve_limit:
            return RiskDecision(False, "minimum virtual cash reserve would be violated")
        return RiskDecision(True, "allowed")
