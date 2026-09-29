from __future__ import annotations

import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.evaluation import evaluate
from autosport.paper import PaperBook


class EvaluationAggregateExponentIntegrityTests(unittest.TestCase):
    def test_settled_stake_carry_beyond_emax_fails_closed(self) -> None:
        book = PaperBook(Decimal("9E+999999"))

        won_leg = TicketLeg(
            "event-emax-won",
            "winner",
            "player-a",
            Decimal("1.1"),
        )
        won_ticket = book.open_ticket(
            [won_leg],
            Decimal("9E+999999"),
            reason="aggregate Emax carry regression won",
        )
        book.settle(won_ticket.ticket_id, {won_leg.quote_key})
        self.assertEqual(book.balance, Decimal("9.9E+999999"))

        lost_leg = TicketLeg(
            "event-emax-lost",
            "winner",
            "player-b",
            Decimal("1.1"),
        )
        lost_ticket = book.open_ticket(
            [lost_leg],
            Decimal("9E+999999"),
            reason="aggregate Emax carry regression lost",
        )
        self.assertEqual(book.balance, Decimal("9E+999998"))
        book.settle(lost_ticket.ticket_id, set())
        self.assertEqual(book.balance, Decimal("9E+999998"))

        with self.assertRaisesRegex(
            ValueError,
            "virtual bankroll state is invalid for evaluation",
        ):
            evaluate(book)


if __name__ == "__main__":
    unittest.main()
