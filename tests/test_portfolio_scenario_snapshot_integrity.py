from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from autosport.domain import TicketStatus, TicketLeg
from autosport.paper import PaperBook
import autosport.portfolio as portfolio_module
from autosport.portfolio import PortfolioEngine


class PortfolioScenarioSnapshotIntegrityTests(unittest.TestCase):
    @staticmethod
    def _open_ticket():
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-21T08:00:00+00:00",
        )
        return book, ticket, leg

    def test_exact_analysis_is_stable_if_ticket_settles_after_state_derivation(self) -> None:
        book, ticket, leg = self._open_ticket()

        class SettlingExactEngine(PortfolioEngine):
            def _exact_profits(self, tickets, groups, ungrouped):
                book.settle(
                    ticket.ticket_id,
                    {leg.quote_key},
                    settled_at="2026-09-21T08:00:01+00:00",
                )
                return super()._exact_profits(tickets, groups, ungrouped)

        report = SettlingExactEngine().analyse([ticket])

        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(report.mode, "exact")
        self.assertEqual(report.scenario_count, 2)
        self.assertEqual(report.worst_case, Decimal("-10"))
        self.assertEqual(report.best_case, Decimal("10"))
        self.assertEqual(report.mean_case, Decimal("0"))

    def test_sampled_analysis_is_stable_if_ticket_settles_after_state_derivation(self) -> None:
        book, ticket, leg = self._open_ticket()

        class SettlingSampleEngine(PortfolioEngine):
            def _sample_profits(self, tickets, groups, ungrouped):
                book.settle(
                    ticket.ticket_id,
                    {leg.quote_key},
                    settled_at="2026-09-21T08:00:01+00:00",
                )
                return super()._sample_profits(tickets, groups, ungrouped)

        report = SettlingSampleEngine(
            max_exact_states=1,
            sample_count=8,
            seed=7,
        ).analyse([ticket])

        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(report.mode, "approximate")
        self.assertEqual(report.scenario_count, 8)
        self.assertEqual(report.worst_case, Decimal("-10"))
        self.assertEqual(report.best_case, Decimal("10"))

    def test_snapshot_fails_closed_if_settlement_crosses_capture_window(self) -> None:
        book = PaperBook("100")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "10",
            placed_at="2026-09-21T08:00:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "10",
            placed_at="2026-09-21T08:00:00+00:00",
        )
        copies = 0
        canonical_fingerprint = portfolio_module._analysis_ticket_fingerprint

        def fingerprint_then_settle(ticket):
            nonlocal copies
            snapshot = canonical_fingerprint(ticket)
            copies += 1
            if copies == 1:
                # The unsafe interleaving is deterministic:
                # first was observed OPEN, then both source tickets settle
                # before the second source ticket is inspected.
                book.settle(
                    first.ticket_id,
                    {first_leg.quote_key},
                    settled_at="2026-09-21T08:00:01+00:00",
                )
                book.settle(
                    second.ticket_id,
                    {second_leg.quote_key},
                    settled_at="2026-09-21T08:00:02+00:00",
                )
            return snapshot

        with patch.object(
            portfolio_module,
            "_analysis_ticket_fingerprint",
            side_effect=fingerprint_then_settle,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "portfolio ticket changed during snapshot",
            ):
                PortfolioEngine().analyse([first, second])

        self.assertEqual(first.status, TicketStatus.WON)
        self.assertEqual(second.status, TicketStatus.WON)


if __name__ == "__main__":
    unittest.main()
