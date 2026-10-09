"""Plan 2 Section 7: isolated readback/conservation/negative/restart qualification."""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.financial_reconciliation import (
    FinancialReconciliationView,
    read_paper_financial_state,
    read_real_financial_state,
)
from autosport.paper import PaperBook
from autosport.real_execution_ledger import RealExecutionLedger


class Plan2FinancialReconciliationTests(unittest.TestCase):
    def test_paper_conservation_restart_and_duplicate_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            book = PaperBook("100.00")
            book.save(path)
            initial = read_paper_financial_state(path)
            self.assertEqual((initial.cash_balance, initial.open_stake, initial.realized_pnl),
                             (Decimal("100.00"), Decimal("0"), Decimal("0.00")))
            self.assertEqual(initial, read_paper_financial_state(path))
            ticket = book.open_ticket(
                [TicketLeg(event_id="evt-1", market_id="winner",
                           selection_id="home", locked_odds=Decimal("2.5"))],
                Decimal("10.00"),
            )
            book.save(path)
            open_state = read_paper_financial_state(path)
            self.assertEqual(open_state.open_obligation_ids, (ticket.ticket_id,))
            self.assertEqual(open_state.cash_balance + open_state.open_stake,
                             Decimal("100.00"))
            self.assertEqual(open_state.realized_pnl, Decimal("0.00"))
            book.settle(ticket.ticket_id, {ticket.legs[0].quote_key})
            book.save(path)
            settled = read_paper_financial_state(path)
            self.assertEqual(settled.cash_balance, Decimal("115.000"))
            self.assertEqual(settled.realized_pnl, Decimal("15.000"))
            self.assertEqual(settled.open_obligation_ids, ())
            self.assertEqual(settled, read_paper_financial_state(path))
            self.assertEqual(len(book.tickets), 1)

    def test_malformed_snapshot_fails_closed_and_does_not_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            payload = b'{"balance":"100","balance":"999"}'
            path.write_bytes(payload)
            with self.assertRaises(ValueError):
                read_paper_financial_state(path)
            self.assertEqual(path.read_bytes(), payload)

    def test_real_empty_plan_is_not_cash_or_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            with self.assertRaises(KeyError):
                read_real_financial_state(ledger, plan_id="missing")
            with self.assertRaises(ValueError):
                FinancialReconciliationView(
                    evidence_class="REAL_LEDGER_READ_ONLY", source_sha256="a" * 64,
                    currency="EUR", cash_balance=Decimal("1"),
                    open_stake=None, realized_pnl=None,
                    open_obligation_ids=(), accepted_ack_not_fill_ids=(),
                )

    def test_hostile_money_and_fake_external_authority_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FinancialReconciliationView(
                evidence_class="PAPER_VIRTUAL", source_sha256="a" * 64,
                currency="EUR", cash_balance=1.0, open_stake=Decimal("0"),
                realized_pnl=Decimal("0"), open_obligation_ids=(),
                accepted_ack_not_fill_ids=(),
            )
        with self.assertRaises(ValueError):
            FinancialReconciliationView(
                evidence_class="PAPER_VIRTUAL", source_sha256="a" * 64,
                currency="EUR", cash_balance=Decimal("0"), open_stake=Decimal("0"),
                realized_pnl=Decimal("0"), open_obligation_ids=(),
                accepted_ack_not_fill_ids=(), externally_authoritative=True,
            )


if __name__ == "__main__":
    unittest.main()
