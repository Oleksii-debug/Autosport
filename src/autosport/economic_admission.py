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
    if type(value) is Decimal:
        amount = value
    elif type(value) is str:
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("stake must be a finite positive decimal") from exc
    else:
        raise ValueError("stake must be a finite positive decimal")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("stake must be a finite positive decimal")
    return amount


def _sync_book_state(target: PaperBook, source: PaperBook) -> None:
    """Refresh one exact caller view from the validated canonical durable book."""

    PaperBook._validate_loaded_state(source)
    target.initial_bankroll = source.initial_bankroll
    target.balance = source.balance
    target.tickets = dict(source.tickets)
    target._lifecycle = list(source._lifecycle)
    target._settlement_times = dict(source._settlement_times)
    PaperBook._validate_loaded_state(target)


def _validate_context_binding(
    *,
    context: ProposedTicketRiskContext | None,
    legs: tuple[TicketLeg, ...],
    provider_source_ids: tuple[str, ...],
    provider_accounts: tuple[tuple[str, str], ...],
    bankroll_id: str | None,
    currency: str | None,
) -> None:
    """Bind the risk-reviewed proposal to the exact ticket that will be opened."""

    if context is None:
        return
    if context.legs != legs:
        raise ValueError("risk context legs must match admitted ticket legs")
    if context.provider_accounts != provider_accounts:
        raise ValueError(
            "risk context provider accounts must match admitted ticket provenance"
        )
    if context.bankroll_id != bankroll_id or context.currency != currency:
        raise ValueError(
            "risk context bankroll and currency must match admitted ticket provenance"
        )
    if context.quotes:
        quote_source_ids = tuple(sorted({quote.source_id for quote in context.quotes}))
        if quote_source_ids != provider_source_ids:
            raise ValueError(
                "risk context quote sources must match admitted ticket provider sources"
            )


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

    if type(book) is not PaperBook:
        raise TypeError("book must be an exact PaperBook")
    if type(risk_policy) is not PaperRiskPolicy:
        raise TypeError("risk_policy must be an exact PaperRiskPolicy")
    if context is not None and type(context) is not ProposedTicketRiskContext:
        raise TypeError("context must be an exact ProposedTicketRiskContext or None")
    if type(legs) is not tuple or not legs:
        raise ValueError("legs must be a non-empty canonical tuple")

    amount = _positive_decimal(stake)
    _validate_context_binding(
        context=context,
        legs=legs,
        provider_source_ids=provider_source_ids,
        provider_accounts=provider_accounts,
        bankroll_id=bankroll_id,
        currency=currency,
    )

    root = Path(workspace).expanduser().resolve(strict=False)
    book_path = root / "paper_book.json"

    with WorkspaceEconomicLock(root):
        # The lock alone is insufficient if this caller was constructed before a
        # different process committed a newer PaperBook. Re-read the one durable
        # workspace book only after owning the economic writer lock.
        if not book_path.exists():
            raise FileNotFoundError(
                "canonical paper_book.json must already exist; "
                "bootstrap/recovery belongs to the product lifecycle"
            )
        canonical_book = PaperBook.load(book_path)

        decision = risk_policy.evaluate(canonical_book, amount, context=context)
        if not decision.allowed:
            _sync_book_state(book, canonical_book)
            return PaperAdmissionResult(risk=decision, ticket=None)

        opened = canonical_book.open_ticket(
            legs,
            amount,
            reason=reason,
            placed_at=placed_at,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        # Publish the mutation while the same lock is still held. PaperBook.save
        # uses atomic replacement; a save failure leaves the prior durable state
        # intact and the caller view has not yet been mutated.
        canonical_book.save(book_path)
        persisted = PaperBook.load(book_path)
        persisted_ticket = persisted.tickets.get(opened.ticket_id)
        if persisted_ticket is None:
            raise RuntimeError(
                "persisted PaperBook lost the ticket opened inside admission"
            )
        _sync_book_state(book, persisted)
        return PaperAdmissionResult(
            risk=decision,
            ticket=book.tickets[persisted_ticket.ticket_id],
        )
