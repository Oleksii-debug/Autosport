from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .domain import PaperTicket, TicketLeg
from .paper import PaperBook
from .risk import PaperRiskPolicy, ProposedTicketRiskContext, RiskDecision
from .workspace_lock import WorkspaceEconomicLock


@dataclass(frozen=True, slots=True)
class PaperAdmissionResult:
    """Result of one risk-evaluate + PAPER ticket-open critical section."""

    risk: RiskDecision
    ticket: PaperTicket | None

    @property
    def admitted(self) -> bool:
        return self.ticket is not None


def _positive_decimal(value: Decimal | str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("stake must be a finite positive decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("stake must be a finite positive decimal")
    return amount


def admit_paper_ticket(
    *,
    workspace: str | Path,
    book: PaperBook,
    risk_policy: PaperRiskPolicy,
    stake: Decimal | str,
    legs: tuple[TicketLeg, ...],
    reason: str,
    placed_at: str,
    context: ProposedTicketRiskContext | None = None,
    provider_source_ids: tuple[str, ...] = (),
    provider_accounts: tuple[tuple[str, str], ...] = (),
    bankroll_id: str | None = None,
    currency: str | None = None,
) -> PaperAdmissionResult:
    """Atomically evaluate canonical PAPER risk and open the ticket if allowed.

    This function does not create a second risk, turnover, reservation, or ledger
    authority.  It only composes the existing PaperRiskPolicy + PaperBook mutation
    inside the canonical WorkspaceEconomicLock so cooperating writers cannot both
    consume the same stale pre-action risk/headroom snapshot.

    The lock is intentionally acquired *before* risk evaluation.  A contender that
    cannot acquire the lock fails closed through WorkspaceEconomicLock rather than
    evaluating against stale economic state.
    """

    if not isinstance(book, PaperBook):
        raise TypeError("book must be a PaperBook")
    if not isinstance(risk_policy, PaperRiskPolicy):
        raise TypeError("risk_policy must be a PaperRiskPolicy")
    if type(legs) is not tuple or not legs:
        raise ValueError("legs must be a non-empty canonical tuple")

    amount = _positive_decimal(stake)

    with WorkspaceEconomicLock(workspace):
        decision = risk_policy.evaluate(book, amount, context=context)
        if not decision.allowed:
            return PaperAdmissionResult(risk=decision, ticket=None)

        ticket = book.open_ticket(
            legs,
            amount,
            reason=reason,
            placed_at=placed_at,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        return PaperAdmissionResult(risk=decision, ticket=ticket)
