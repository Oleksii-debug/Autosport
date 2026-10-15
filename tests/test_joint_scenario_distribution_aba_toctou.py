from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from autosport.domain import TicketLeg
from autosport.joint_scenario_distribution import (
    JointScenarioState,
    analyse_joint_distribution,
)
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


class JointScenarioDistributionAbaToctouTests(unittest.TestCase):
    def test_transient_ticket_stake_mutation_cannot_escape_final_hash_check(self) -> None:
        book = PaperBook("100")
        a1 = TicketLeg("e1", "winner", "a", Decimal("2"))
        b1 = TicketLeg("e1", "winner", "b", Decimal("2"))
        a2 = TicketLeg("e2", "winner", "a", Decimal("2"))
        b2 = TicketLeg("e2", "winner", "b", Decimal("2"))
        ticket = book.open_ticket([a1, a2], "10")

        groups = (
            ScenarioGroup(
                "e1-winner",
                (
                    ScenarioOutcome(a1.quote_key, Decimal("0.5")),
                    ScenarioOutcome(b1.quote_key, Decimal("0.5")),
                ),
            ),
            ScenarioGroup(
                "e2-winner",
                (
                    ScenarioOutcome(a2.quote_key, Decimal("0.5")),
                    ScenarioOutcome(b2.quote_key, Decimal("0.5")),
                ),
            ),
        )
        states = (
            JointScenarioState(
                "both-a",
                (a1.quote_key, a2.quote_key),
                Decimal("0.5"),
            ),
            JointScenarioState(
                "both-b",
                (b1.quote_key, b2.quote_key),
                Decimal("0.5"),
            ),
        )

        canonical_before = ticket.stake
        original = PortfolioEngine.scenario_profit
        calls = 0

        def aba_profit(tickets, winning_quote_keys):
            nonlocal calls
            calls += 1
            if calls != 1:
                return original(tickets, winning_quote_keys)

            # PaperTicket is mutable. Change an economic input only while the
            # first scenario is evaluated, then restore the exact canonical
            # value before analyse_joint_distribution performs its final hash.
            target = tickets[0]
            previous_stake = target.stake
            target.stake = Decimal("20")
            try:
                return original(tickets, winning_quote_keys)
            finally:
                target.stake = previous_stake

        with patch.object(PortfolioEngine, "scenario_profit", side_effect=aba_profit):
            with self.assertRaisesRegex(
                ValueError,
                "changed during joint scenario analysis",
            ):
                analyse_joint_distribution(book, groups, states)

        self.assertEqual(ticket.stake, canonical_before)


if __name__ == "__main__":
    unittest.main()
