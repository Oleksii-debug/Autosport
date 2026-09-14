import tempfile
import unittest
from pathlib import Path

from autosport.paper import PaperBook


class PaperBookJsonIntegrityTests(unittest.TestCase):
    def test_duplicate_root_key_is_rejected_even_when_values_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper_book.json"
            path.write_text(
                '{"initial_bankroll":"100","balance":"100","balance":"100","tickets":[]}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate JSON key: balance"):
                PaperBook.load(path)

    def test_duplicate_nested_ticket_key_cannot_select_plausible_economics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper_book.json"
            path.write_text(
                """{
  "initial_bankroll": "100",
  "balance": "90",
  "tickets": [
    {
      "ticket_id": "ticket-1",
      "stake": "20",
      "stake": "10",
      "placed_at": "2026-09-14T00:00:00+00:00",
      "status": "open",
      "payout": "0",
      "strategy_reason": "test",
      "legs": [
        {
          "event_id": "event-1",
          "market_id": "market-1",
          "selection_id": "selection-1",
          "locked_odds": "2"
        }
      ]
    }
  ]
}
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate JSON key: stake"):
                PaperBook.load(path)


if __name__ == "__main__":
    unittest.main()
