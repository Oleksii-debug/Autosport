import unittest
from decimal import Decimal

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.settlement import SettlementEngine


class SettlementKnownOutcomeLateTicketCausalityTests(unittest.TestCase):
    @staticmethod
    def _leg() -> TicketLeg:
        return TicketLeg("event-1", "winner", "alice", Decimal("2"))

    def test_preexisting_ticket_can_settle_when_outcome_arrives(self) -> None:
        book = PaperBook("100")
        leg = self._leg()
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-22T07:00:00+00:00",
        )
        settlement = SettlementEngine()

        settlement.record({leg.quote_key: "win"})

        self.assertEqual(settlement.settle_ready(book), [ticket.ticket_id])
        self.assertIs(ticket.status, TicketStatus.WON)
        self.assertEqual(ticket.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))

    def test_settled_outcome_cannot_settle_ticket_opened_after_outcome_was_known(self) -> None:
        book = PaperBook("100")
        leg = self._leg()
        first = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-22T07:00:00+00:00",
        )
        settlement = SettlementEngine()
        settlement.record({leg.quote_key: "win"})
        self.assertEqual(settlement.settle_ready(book), [first.ticket_id])
        self.assertIs(first.status, TicketStatus.WON)

        late = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-22T07:01:00+00:00",
        )
        balance_before = book.balance
        lifecycle_before = list(book._lifecycle)

        try:
            settled = settlement.settle_ready(book)
        except ValueError:
            self.assertEqual(book.balance, balance_before)
            self.assertIs(late.status, TicketStatus.OPEN)
            self.assertEqual(late.payout, Decimal("0"))
            self.assertEqual(book._lifecycle, lifecycle_before)
            return

        self.assertEqual(
            settled,
            [],
            "already-known settlement truth must not authorize a later PAPER ticket",
        )
        self.assertEqual(book.balance, balance_before)
        self.assertIs(late.status, TicketStatus.OPEN)
        self.assertEqual(late.payout, Decimal("0"))
        self.assertEqual(book._lifecycle, lifecycle_before)


if __name__ == "__main__":
    unittest.main()
