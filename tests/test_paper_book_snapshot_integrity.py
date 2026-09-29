import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class PaperBookSnapshotIntegrityTests(unittest.TestCase):
    def _snapshot(self, raw: dict) -> Path:
        root = Path(self._tmp.name)
        path = root / "paper_book.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip_preserves_valid_open_and_settled_state(self):
        book = PaperBook("100")
        won_leg = TicketLeg("e1", "m", "a", locked_odds=Decimal("2"))
        open_leg = TicketLeg("e2", "m", "b", locked_odds=Decimal("1.5"))
        won = book.open_ticket([won_leg], "10")
        book.settle(won.ticket_id, {won_leg.quote_key})
        book.open_ticket([open_leg], "5")
        path = Path(self._tmp.name) / "paper_book.json"
        book.save(path)

        restored = PaperBook.load(path)

        self.assertEqual(restored.balance, book.balance)
        self.assertEqual(restored.committed_stake, book.committed_stake)
        self.assertEqual(set(restored.tickets), set(book.tickets))

    def test_open_ticket_rejects_duplicate_quote_key_without_mutating_bankroll(self):
        book = PaperBook("100")
        first = TicketLeg("e", "m", "a", locked_odds=Decimal("2"))
        duplicate = TicketLeg("e", "m", "a", locked_odds=Decimal("3"))

        with self.assertRaisesRegex(ValueError, "duplicate quote_key"):
            book.open_ticket((leg for leg in [first, duplicate]), "10")

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_load_rejects_balance_not_explained_by_ticket_economics(self):
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "99",
                "tickets": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "balance is inconsistent"):
            PaperBook.load(path)

    def test_load_rejects_duplicate_ticket_identity_instead_of_overwriting(self):
        ticket = {
            "ticket_id": "same",
            "stake": "10",
            "placed_at": "2026-01-01T00:00:00+00:00",
            "status": "open",
            "payout": "0",
            "strategy_reason": "test",
            "legs": [
                {
                    "event_id": "e",
                    "market_id": "m",
                    "selection_id": "a",
                    "locked_odds": "2",
                }
            ],
        }
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "80",
                "tickets": [ticket, dict(ticket)],
            }
        )
        with self.assertRaisesRegex(ValueError, "duplicate ticket_id"):
            PaperBook.load(path)

    def test_load_rejects_duplicate_quote_key_legs(self):
        leg = {
            "event_id": "e",
            "market_id": "m",
            "selection_id": "a",
            "locked_odds": "2",
        }
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "90",
                "tickets": [
                    {
                        "ticket_id": "duplicate-leg",
                        "stake": "10",
                        "placed_at": "2026-01-01T00:00:00+00:00",
                        "status": "open",
                        "payout": "0",
                        "strategy_reason": "test",
                        "legs": [leg, dict(leg)],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "duplicate quote_key"):
            PaperBook.load(path)

    def test_load_rejects_impossible_status_payout(self):
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "95",
                "tickets": [
                    {
                        "ticket_id": "lost-with-payout",
                        "stake": "10",
                        "placed_at": "2026-01-01T00:00:00+00:00",
                        "status": "lost",
                        "payout": "5",
                        "strategy_reason": "test",
                        "legs": [
                            {
                                "event_id": "e",
                                "market_id": "m",
                                "selection_id": "a",
                                "locked_odds": "2",
                            }
                        ],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "open/lost ticket payout must be zero"):
            PaperBook.load(path)

    def test_load_rejects_non_finite_economic_values(self):
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "Infinity",
                "tickets": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "non-finite balance"):
            PaperBook.load(path)


if __name__ == "__main__":
    unittest.main()
