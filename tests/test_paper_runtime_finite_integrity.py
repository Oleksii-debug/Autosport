import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class PaperBookRuntimeFiniteIntegrityTests(unittest.TestCase):
    def test_constructor_rejects_non_finite_or_non_positive_virtual_bankroll(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "non-finite initial_bankroll"):
                    PaperBook(value)
        for value in ("0", "-1"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "initial virtual bankroll must be positive"):
                    PaperBook(value)

    def test_open_ticket_rejects_non_finite_stake_before_mutating_book(self) -> None:
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                book = PaperBook("100")
                with self.assertRaisesRegex(ValueError, "non-finite stake"):
                    book.open_ticket([leg], value)
                self.assertEqual(book.balance, Decimal("100"))
                self.assertEqual(book.tickets, {})

    def test_open_ticket_rejects_non_finite_locked_odds_before_mutating_book(self) -> None:
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                book = PaperBook("100")
                leg = TicketLeg("event", "winner", "alice", value)
                with self.assertRaisesRegex(ValueError, "non-finite locked_odds"):
                    book.open_ticket([leg], "10")
                self.assertEqual(book.balance, Decimal("100"))
                self.assertEqual(book.tickets, {})

    def test_valid_runtime_book_remains_saveable_and_loadable(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        self.assertEqual(ticket.stake, Decimal("10"))
        self.assertEqual(book.balance, Decimal("90"))


if __name__ == "__main__":
    unittest.main()
