import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class PaperTicketTimestampIntegrityTests(unittest.TestCase):
    @staticmethod
    def _leg() -> TicketLeg:
        return TicketLeg("event", "market", "selection", locked_odds=Decimal("2"))

    def test_open_ticket_preserves_explicit_timezone_aware_timestamp(self):
        book = PaperBook("100")
        placed_at = "2026-09-14T09:00:00Z"

        ticket = book.open_ticket([self._leg()], "10", placed_at=placed_at)

        self.assertEqual(ticket.placed_at, placed_at)
        self.assertEqual(book.balance, Decimal("90"))

    def test_open_ticket_rejects_invalid_timestamp_without_mutating_state(self):
        invalid_values = (
            "",
            " 2026-09-14T09:00:00+00:00",
            "2026-09-14T09:00:00",
            0,
        )
        for invalid in invalid_values:
            with self.subTest(placed_at=invalid):
                book = PaperBook("100")

                with self.assertRaisesRegex(ValueError, "placed_at"):
                    book.open_ticket([self._leg()], "10", placed_at=invalid)  # type: ignore[arg-type]

                self.assertEqual(book.balance, Decimal("100"))
                self.assertEqual(book.tickets, {})

    def test_load_rejects_invalid_persisted_timestamp(self):
        invalid_values = ("", "2026-09-14T09:00:00", 0)
        for invalid in invalid_values:
            with self.subTest(placed_at=invalid), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "paper_book.json"
                raw = {
                    "initial_bankroll": "100",
                    "balance": "90",
                    "tickets": [
                        {
                            "ticket_id": "ticket",
                            "stake": "10",
                            "placed_at": invalid,
                            "status": "open",
                            "payout": "0",
                            "strategy_reason": "test",
                            "legs": [
                                {
                                    "event_id": "event",
                                    "market_id": "market",
                                    "selection_id": "selection",
                                    "locked_odds": "2",
                                }
                            ],
                        }
                    ],
                }
                path.write_text(json.dumps(raw), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "snapshot placed_at"):
                    PaperBook.load(path)

    def test_save_rejects_mutated_invalid_timestamp_without_replacing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper_book.json"
            book = PaperBook("100")
            ticket = book.open_ticket(
                [self._leg()],
                "10",
                placed_at="2026-09-14T09:00:00+00:00",
            )
            book.save(path)
            original = path.read_text(encoding="utf-8")
            ticket.placed_at = ""

            with self.assertRaisesRegex(ValueError, "snapshot placed_at"):
                book.save(path)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
