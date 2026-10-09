"""Read-only, currency-explicit Plan-2 paper/real reconciliation projection.

The canonical ledgers remain sole truth owners. No DTO, result or caller-supplied
amount from this module grants Risk, settlement, execution or provider authority.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, Inexact, localcontext
from pathlib import Path

from .domain import TicketStatus
from .paper import PaperBook
from .real_execution_ledger import RealExecutionLedger


@dataclass(frozen=True, slots=True)
class FinancialReconciliationView:
    """One typed interface with deliberately different PAPER/REAL evidence grades.

    Monetary fields use virtual-bankroll units only in PAPER mode; in REAL
    mode they are absent rather than fabricated from request/ACK amounts.
    """

    evidence_class: str
    source_sha256: str
    currency: str | None
    cash_balance: Decimal | None
    open_stake: Decimal | None
    realized_pnl: Decimal | None
    open_obligation_ids: tuple[str, ...]
    accepted_ack_not_fill_ids: tuple[str, ...]
    externally_authoritative: bool = False

    def __post_init__(self) -> None:
        if self.evidence_class not in {"PAPER_VIRTUAL", "REAL_LEDGER_READ_ONLY"}:
            raise ValueError("unknown financial evidence class")
        if type(self.source_sha256) is not str or len(self.source_sha256) != 64:
            raise ValueError("source digest must be SHA-256")
        if type(self.open_obligation_ids) is not tuple or type(self.accepted_ack_not_fill_ids) is not tuple:
            raise ValueError("obligation identifiers must be immutable tuples")
        if any(type(item) is not str or not item for item in self.open_obligation_ids + self.accepted_ack_not_fill_ids):
            raise ValueError("invalid obligation identifier")
        if type(self.externally_authoritative) is not bool or self.externally_authoritative:
            raise ValueError("a read model cannot grant external authority")
        if self.evidence_class == "REAL_LEDGER_READ_ONLY":
            if any(item is not None for item in (self.currency, self.cash_balance, self.open_stake, self.realized_pnl)):
                raise ValueError("real ACK/receipt cannot mint cash, fill or P&L")
        else:
            for field in (self.cash_balance, self.open_stake, self.realized_pnl):
                if type(field) is not Decimal or not field.is_finite():
                    raise ValueError("PAPER monetary values require finite exact Decimal")
            if self.cash_balance < 0 or self.open_stake < 0:
                raise ValueError("negative PAPER cash/open stake")
            if self.accepted_ack_not_fill_ids:
                raise ValueError("PAPER read model cannot contain real ACKs")


def read_paper_financial_state(path: str | Path) -> FinancialReconciliationView:
    """Reload the canonical validated PaperBook; never trust in-memory ticket DTOs.

    A read-only double-read rejects a changed snapshot rather than returning a
    plausible monetary total computed from different persistence generations.
    Writer serialization is owned by the existing PaperBook/workspace machinery.
    """
    location = Path(path)
    before = location.read_bytes()
    book = PaperBook.load(location)
    after = location.read_bytes()
    if before != after:
        raise ValueError("PAPER snapshot changed during financial reconciliation")
    if type(book) is not PaperBook:
        raise ValueError("unexpected PAPER authority type")
    # PaperBook's own authority and lifecycle guards execute at this boundary.
    open_stake = book.committed_stake
    currencies = {ticket.currency for ticket in book.tickets.values()}
    if len(currencies) > 1:
        raise ValueError("mixed PAPER currencies cannot be summed")
    with localcontext() as context:
        context.traps[Inexact] = True
        realized = (book.balance + open_stake) - book.initial_bankroll
    open_ids = tuple(sorted(
        ticket_id for ticket_id, ticket in book.tickets.items()
        if ticket.status is TicketStatus.OPEN
    ))
    return FinancialReconciliationView(
        evidence_class="PAPER_VIRTUAL",
        source_sha256=hashlib.sha256(before).hexdigest(),
        currency=next(iter(currencies)) if currencies else None,
        cash_balance=book.balance,
        open_stake=open_stake,
        realized_pnl=realized,
        open_obligation_ids=open_ids,
        accepted_ack_not_fill_ids=(),
    )


def read_real_financial_state(
    ledger: RealExecutionLedger, *, plan_id: str
) -> FinancialReconciliationView:
    """Expose uncertain/acknowledged work, never claimed matched real exposure.

    The existing ledger's verified_execution_view supplies one immutable
    digest-bound cut. Accepted/PARTIAL acknowledgement is *not* provider fill.
    The real account's currency, cash and settlement require independent proof.
    """
    if type(ledger) is not RealExecutionLedger:
        raise ValueError("real reconciliation needs canonical ledger")
    view = ledger.verified_execution_view(plan_id)
    if view.stale:
        raise ValueError("stale execution plan cannot be a current reconciliation cut")
    # A plan can have no attempts; planned actions never become open positions.
    unresolved = tuple(sorted(item.attempt.attempt_id for item in view.attempts
                              if item.state.value != "RECONCILED_NOT_FOUND"))
    acknowledged = tuple(sorted(item.attempt.attempt_id for item in view.attempts
                                if item.acknowledgement is not None))
    return FinancialReconciliationView(
        evidence_class="REAL_LEDGER_READ_ONLY",
        source_sha256=view.snapshot_sha256,
        currency=None, cash_balance=None, open_stake=None, realized_pnl=None,
        open_obligation_ids=unresolved,
        accepted_ack_not_fill_ids=acknowledged,
    )
