import tempfile
import unittest
from decimal import Decimal, Inexact, localcontext
from pathlib import Path

from autosport.domain import TicketLeg, TicketStatus
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

    def test_subprecision_stake_rejection_is_caller_context_isolated(self) -> None:
        for precision in (6, 60):
            with self.subTest(precision=precision), localcontext() as caller:
                caller.prec = precision
                caller.clear_flags()

                book = PaperBook("1")
                leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
                with self.assertRaisesRegex(
                    ValueError,
                    "stake debit loses Decimal precision",
                ):
                    book.open_ticket(
                        [leg],
                        Decimal("1E-29"),
                        placed_at="2026-09-14T09:00:00+00:00",
                    )

                self.assertEqual(book.balance, Decimal("1"))
                self.assertEqual(book.tickets, {})
                self.assertEqual(caller.prec, precision)
                self.assertFalse(caller.flags[Inexact])

    def test_settlement_is_caller_context_isolated(self) -> None:
        outcomes = []
        for precision in (6, 60):
            with self.subTest(precision=precision), localcontext() as caller:
                caller.prec = precision
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
                outcomes.append((ticket.payout, book.balance))

                self.assertEqual(caller.prec, precision)
                self.assertFalse(caller.flags[Inexact])

        expected = (
            Decimal("1015.408666917098134398627632"),
            Decimal("10891.95866691709813439862763"),
        )
        self.assertEqual(outcomes, [expected, expected])

    def test_swallowed_settlement_payout_is_atomic_fail_closed(self) -> None:
        book = PaperBook("1")
        small_leg = TicketLeg("event-small", "winner", "alice", Decimal("2"))
        large_leg = TicketLeg("event-large", "winner", "bob", Decimal("1E+28"))
        small = book.open_ticket(
            [small_leg],
            "0.1",
            placed_at="2026-09-14T09:00:00+00:00",
        )
        large = book.open_ticket(
            [large_leg],
            "0.9",
            placed_at="2026-09-14T09:01:00+00:00",
        )
        book.settle(large.ticket_id, {large_leg.quote_key})
        before_balance = book.balance

        with self.assertRaisesRegex(
            ValueError,
            "settlement payout loses all Decimal balance effect",
        ):
            book.settle(small.ticket_id, {small_leg.quote_key})

        self.assertEqual(book.balance, before_balance)
        self.assertIs(small.status, TicketStatus.OPEN)
        self.assertEqual(small.payout, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
