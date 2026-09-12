import tempfile
import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.ui_model import ticket_lines


class UiModelTests(unittest.TestCase):
    def test_ticket_lines_are_textual_and_screen_reader_friendly(self):
        book = PaperBook("100")
        book.open_ticket([TicketLeg("e", "winner", "a", Decimal("2"))], "10")

        class Session:
            pass

        session = Session()
        session.book = book
        lines = ticket_lines(session)
        self.assertEqual(len(lines), 1)
        self.assertIn("OPEN", lines[0])
        self.assertIn("stake 10", lines[0])
        self.assertIn("e/winner/a@2", lines[0])


if __name__ == "__main__":
    unittest.main()
