from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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

    def evaluate(self, book: PaperBook, stake: Decimal | str) -> RiskDecision:
        amount = Decimal(str(stake))
        if amount <= 0:
            return RiskDecision(False, "stake must be positive")
        if amount > book.initial_bankroll * self.max_ticket_fraction:
            return RiskDecision(False, "ticket exceeds configured bankroll fraction")
        if book.committed_stake + amount > book.initial_bankroll * self.max_committed_fraction:
            return RiskDecision(False, "aggregate committed stake limit exceeded")
        if book.balance - amount < book.initial_bankroll * self.minimum_cash_reserve_fraction:
            return RiskDecision(False, "minimum virtual cash reserve would be violated")
        return RiskDecision(True, "allowed")
