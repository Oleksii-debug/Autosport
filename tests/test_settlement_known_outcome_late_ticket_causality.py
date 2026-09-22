import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
)
from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook


class ContinuousSettlementTicketCausalityTests(unittest.TestCase):
    @staticmethod
    def _coordinator(root: Path) -> ContinuousSessionCoordinator:
        coordinator = object.__new__(ContinuousSessionCoordinator)
        coordinator.workspace = root
        coordinator.paper_book_path = root / "paper_book.json"
        coordinator.initial_bankroll = "100"
        return coordinator

    @staticmethod
    def _resolution(leg: TicketLeg, *, available_at: str) -> SettlementResolution:
        return SettlementResolution(
            event_identity="provider-a:event-1",
            settlement_ref="provider-result:1",
            quote_outcomes={leg.quote_key: "win"},
            evidence_id="outcome-1",
            evidence_sha256="0" * 64,
            available_at=available_at,
        )

    def test_outcome_available_after_ticket_placement_can_settle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book = PaperBook("100")
            leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
            ticket = book.open_ticket(
                [leg],
                "10",
                placed_at="2026-09-22T07:00:00+00:00",
            )
            book.save(root / "paper_book.json")
            coordinator = self._coordinator(root)
            resolution = self._resolution(
                leg,
                available_at="2026-09-22T07:01:00+00:00",
            )

            settled, _evidence_ids = coordinator._settle(resolutions=(resolution,))

            self.assertEqual(settled, (ticket.ticket_id,))
            settled_book = PaperBook.load(root / "paper_book.json")
            self.assertIs(settled_book.tickets[ticket.ticket_id].status, TicketStatus.WON)
            self.assertEqual(settled_book.balance, Decimal("110"))

    def test_outcome_available_before_ticket_placement_cannot_settle_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book = PaperBook("100")
            leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
            ticket = book.open_ticket(
                [leg],
                "10",
                placed_at="2026-09-22T07:01:00+00:00",
            )
            book.save(root / "paper_book.json")
            coordinator = self._coordinator(root)
            resolution = self._resolution(
                leg,
                available_at="2026-09-22T07:00:00+00:00",
            )
            balance_before = book.balance
            lifecycle_before = list(book._lifecycle)

            try:
                settled, _evidence_ids = coordinator._settle(resolutions=(resolution,))
            except (ContinuousSessionError, ValueError):
                after = PaperBook.load(root / "paper_book.json")
                self.assertEqual(after.balance, balance_before)
                self.assertIs(after.tickets[ticket.ticket_id].status, TicketStatus.OPEN)
                self.assertEqual(after._lifecycle, lifecycle_before)
                return

            self.assertEqual(
                settled,
                (),
                "settlement evidence that predates ticket placement must not settle the ticket",
            )
            after = PaperBook.load(root / "paper_book.json")
            self.assertEqual(after.balance, balance_before)
            self.assertIs(after.tickets[ticket.ticket_id].status, TicketStatus.OPEN)
            self.assertEqual(after._lifecycle, lifecycle_before)


if __name__ == "__main__":
    unittest.main()
