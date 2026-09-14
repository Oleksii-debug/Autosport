from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

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
    def _book_state(book: PaperBook) -> tuple[Decimal, Decimal, Decimal] | None:
        """Return canonical finite economic state, or None when risk cannot be evaluated safely."""

        try:
            initial_bankroll = book.initial_bankroll
            balance = book.balance
            committed_stake = book.committed_stake
        except (ArithmeticError, TypeError, ValueError):
            return None
        values = (initial_bankroll, balance, committed_stake)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            return None
        if initial_bankroll <= 0 or balance < 0 or committed_stake < 0:
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
        if amount > initial_bankroll * self.max_ticket_fraction:
            return RiskDecision(False, "ticket exceeds configured bankroll fraction")
        if committed_stake + amount > initial_bankroll * self.max_committed_fraction:
            return RiskDecision(False, "aggregate committed stake limit exceeded")
        if balance - amount < initial_bankroll * self.minimum_cash_reserve_fraction:
            return RiskDecision(False, "minimum virtual cash reserve would be violated")
        return RiskDecision(True, "allowed")
