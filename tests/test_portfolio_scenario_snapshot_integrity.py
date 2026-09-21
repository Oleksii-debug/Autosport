from __future__ import annotations

import unittest
from decimal import Decimal

from autosport.domain import TicketStatus, TicketLeg
from autosport.paper import PaperBook
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
        return ticket

    def test_exact_analysis_is_stable_if_source_ticket_mutates_after_state_derivation(self) -> None:
        ticket = self._open_ticket()

        class MutatingExactEngine(PortfolioEngine):
            def _exact_profits(self, tickets, groups, ungrouped):
                ticket.status = TicketStatus.WON
                ticket.stake = Decimal("999")
                return super()._exact_profits(tickets, groups, ungrouped)

        report = MutatingExactEngine().analyse([ticket])

        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(ticket.stake, Decimal("999"))
        self.assertEqual(report.mode, "exact")
        self.assertEqual(report.scenario_count, 2)
        self.assertEqual(report.worst_case, Decimal("-10"))
        self.assertEqual(report.best_case, Decimal("10"))
        self.assertEqual(report.mean_case, Decimal("0"))

    def test_sampled_analysis_is_stable_if_source_ticket_mutates_after_state_derivation(self) -> None:
        ticket = self._open_ticket()

        class MutatingSampleEngine(PortfolioEngine):
            def _sample_profits(self, tickets, groups, ungrouped):
                ticket.status = TicketStatus.LOST
                ticket.stake = Decimal("999")
                return super()._sample_profits(tickets, groups, ungrouped)

        report = MutatingSampleEngine(
            max_exact_states=1,
            sample_count=8,
            seed=7,
        ).analyse([ticket])

        self.assertEqual(ticket.status, TicketStatus.LOST)
        self.assertEqual(ticket.stake, Decimal("999"))
        self.assertEqual(report.mode, "approximate")
        self.assertEqual(report.scenario_count, 8)
        self.assertEqual(report.worst_case, Decimal("-10"))
        self.assertEqual(report.best_case, Decimal("10"))


if __name__ == "__main__":
    unittest.main()
