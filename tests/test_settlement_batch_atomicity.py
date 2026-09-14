import unittest
from decimal import Decimal

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.settlement import SettlementEngine


class SettlementBatchAtomicityTests(unittest.TestCase):
    def test_later_ready_failure_leaves_entire_batch_unsettled(self) -> None:
        book = PaperBook("9E+999999")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "1E+999999",
            placed_at="2026-09-14T19:30:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "4E+999999",
            placed_at="2026-09-14T19:31:00+00:00",
        )
        settlement = SettlementEngine(
            {
                first_leg.quote_key: "win",
                second_leg.quote_key: "win",
            }
        )
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(
            ValueError, "settlement arithmetic is not representable"
        ):
            settlement.settle_ready(book)

        self.assertEqual(book.balance, before_balance)
        self.assertIs(first.status, TicketStatus.OPEN)
        self.assertEqual(first.payout, Decimal("0"))
        self.assertIs(second.status, TicketStatus.OPEN)
        self.assertEqual(second.payout, Decimal("0"))
        self.assertEqual(book._lifecycle, before_lifecycle)


if __name__ == "__main__":
    unittest.main()
