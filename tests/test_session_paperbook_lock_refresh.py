import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.session import AutosportSession
from autosport.workspace_lock import WorkspaceEconomicLock


class SessionPaperBookLockRefreshTests(unittest.TestCase):
    def _commit_newer_ticket(self, book_path: Path):
        canonical = PaperBook.load(book_path)
        ticket = canonical.open_ticket(
            [TicketLeg("event", "market", "selection", Decimal("2.0"))],
            "100",
            reason="concurrent canonical commit",
            placed_at="2026-01-01T00:00:00+00:00",
        )
        canonical.save(book_path)
        return ticket

    def test_stale_session_refreshes_canonical_paper_book_after_acquiring_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            PaperBook("10000").save(book_path)

            stale_session = AutosportSession(root, "10000")
            try:
                ticket = self._commit_newer_ticket(book_path)

                with WorkspaceEconomicLock(root):
                    stale_session._ensure_canonical_economic_base()

                reloaded = PaperBook.load(book_path)
                self.assertEqual(reloaded.balance, Decimal("9900"))
                self.assertIn(ticket.ticket_id, reloaded.tickets)
                self.assertEqual(stale_session.book.balance, Decimal("9900"))
                self.assertIn(ticket.ticket_id, stale_session.book.tickets)
            finally:
                stale_session.close()

    def test_stale_session_close_does_not_overwrite_newer_canonical_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            PaperBook("10000").save(book_path)

            stale_session = AutosportSession(root, "10000")
            ticket = self._commit_newer_ticket(book_path)

            stale_session.close()

            reloaded = PaperBook.load(book_path)
            self.assertEqual(reloaded.balance, Decimal("9900"))
            self.assertIn(ticket.ticket_id, reloaded.tickets)


if __name__ == "__main__":
    unittest.main()
