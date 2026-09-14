import tempfile
import unittest
from decimal import Decimal, Inexact, localcontext
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class PaperBookDecimalChronologyTests(unittest.TestCase):
    def test_reverse_settlement_order_rounding_round_trips(self) -> None:
        book = PaperBook("10000")
        odds = Decimal("1.234567891234567891234567891")
        first_leg = TicketLeg("event-1", "winner", "alice", odds)
        second_leg = TicketLeg("event-2", "winner", "bob", odds)
        first = book.open_ticket(
            [first_leg],
            "123.45",
            placed_at="2026-09-14T09:00:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "123.45",
            placed_at="2026-09-14T09:01:00+00:00",
        )

        # Settlement chronology is intentionally the reverse of ticket insertion.
        # The snapshot stores final status/payout but has no settled_at/sequence.
        book.settle(second.ticket_id, {second_leg.quote_key})
        book.settle(first.ticket_id, {first_leg.quote_key})
        self.assertEqual(
            book.balance,
            Decimal("10057.91481234581481234581481"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper_book.json"
            book.save(path)
            restored = PaperBook.load(path)

        self.assertEqual(restored.balance, book.balance)
        self.assertEqual(
            tuple(restored.tickets),
            tuple(book.tickets),
        )

    def test_rounding_envelope_still_rejects_material_balance_mutation(self) -> None:
        book = PaperBook("10000")
        odds = Decimal("1.234567891234567891234567891")
        leg = TicketLeg("event-1", "winner", "alice", odds)
        ticket = book.open_ticket(
            [leg],
            "123.45",
            placed_at="2026-09-14T09:00:00+00:00",
        )
        book.settle(ticket.ticket_id, {leg.quote_key})
        book.balance += Decimal("0.01")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper_book.json"
            with self.assertRaisesRegex(ValueError, "balance is inconsistent"):
                book.save(path)
            self.assertFalse(path.exists())

    def test_open_ticket_rejects_stake_debit_that_loses_precision_before_mutation(self) -> None:
        book = PaperBook("1")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))

        with self.assertRaisesRegex(ValueError, "stake debit loses Decimal precision"):
            book.open_ticket(
                [leg],
                Decimal("1E-29"),
                placed_at="2026-09-14T09:00:00+00:00",
            )

        self.assertEqual(book.balance, Decimal("1"))
        self.assertEqual(book.tickets, {})

    def test_paper_arithmetic_isolated_from_caller_decimal_context(self) -> None:
        with localcontext() as caller:
            caller.prec = 6
            caller.clear_flags()

            book = PaperBook("10000")
            legs = tuple(
                TicketLeg(
                    f"event-{index}",
                    "winner",
                    f"player-{index}",
                    Decimal("1.23456789"),
                )
                for index in range(10)
            )
            ticket = book.open_ticket(
                legs,
                "123.45",
                placed_at="2026-09-14T09:00:00+00:00",
            )
            book.settle(
                ticket.ticket_id,
                {leg.quote_key for leg in legs},
            )

            self.assertEqual(
                ticket.payout,
                Decimal("1015.408666917098134398627632"),
            )
            self.assertEqual(
                book.balance,
                Decimal("10891.95866691709813439862763"),
            )
            self.assertEqual(caller.prec, 6)
            self.assertFalse(caller.flags[Inexact])


if __name__ == "__main__":
    unittest.main()
