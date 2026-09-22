from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from autosport.domain import TicketStatus, TicketLeg
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine


class PortfolioSettlementScenarioSnapshotIntegrityTests(unittest.TestCase):
    @staticmethod
    def _two_open_tickets():
        book = PaperBook("100")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "10",
            placed_at="2026-09-22T00:00:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "10",
            placed_at="2026-09-22T00:00:00+00:00",
        )
        settlement = {
            first_leg.quote_key: "win",
            second_leg.quote_key: "win",
        }
        return first, second, settlement

    def test_two_open_winners_have_expected_frozen_cut_profit(self) -> None:
        first, second, settlement = self._two_open_tickets()

        profit = PortfolioEngine.scenario_profit_settlements(
            [first, second],
            settlement,
        )

        self.assertEqual(profit, Decimal("20"))

    def test_later_ticket_settlement_during_evaluation_cannot_change_same_cut_profit(self) -> None:
        first, second, settlement = self._two_open_tickets()
        original = PaperBook._settlement_result
        calls = 0

        def settle_second_after_first_evaluation(
            ticket,
            balance,
            winning_quote_keys,
            void_quote_keys,
        ):
            nonlocal calls
            calls += 1
            result = original(
                ticket,
                balance,
                winning_quote_keys,
                void_quote_keys,
            )
            if calls == 1:
                # Simulate the observable mutation performed by a concurrent
                # PaperBook.settle() after the scenario cut has started.
                second.status = TicketStatus.WON
                second.payout = Decimal("20")
                second.settled_at = "2026-09-22T00:00:01+00:00"
            return result

        with patch.object(
            PaperBook,
            "_settlement_result",
            side_effect=settle_second_after_first_evaluation,
        ):
            profit = PortfolioEngine.scenario_profit_settlements(
                [first, second],
                settlement,
            )

        self.assertEqual(second.status, TicketStatus.WON)
        self.assertEqual(
            profit,
            Decimal("20"),
            "one scenario evaluation must use one frozen open-ticket cut",
        )


if __name__ == "__main__":
    unittest.main()
