import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class PaperBookCanonicalDurableStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "paper_book.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _valid_book(self) -> tuple[PaperBook, object]:
        book = PaperBook("100")
        ticket = book.open_ticket(
            [TicketLeg("event-1", "winner", "alice", Decimal("2"))],
            "10",
            reason="canonical",
        )
        return book, ticket

    def _write_snapshot(self, *, event_id: str, market_id: str, selection_id: str) -> None:
        payload = {
            "initial_bankroll": "100",
            "balance": "90",
            "tickets": [
                {
                    "ticket_id": "ticket-1",
                    "stake": "10",
                    "placed_at": "2026-09-14T00:00:00+00:00",
                    "status": "open",
                    "payout": "0",
                    "strategy_reason": "test",
                    "legs": [
                        {
                            "event_id": event_id,
                            "market_id": market_id,
                            "selection_id": selection_id,
                            "locked_odds": "2",
                        }
                    ],
                }
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_open_ticket_rejects_noncanonical_leg_identity_before_bankroll_mutation(self) -> None:
        invalid_legs = (
            TicketLeg("", "winner", "alice", Decimal("2")),
            TicketLeg(" event-1", "winner", "alice", Decimal("2")),
            TicketLeg("event-1", "winner ", "alice", Decimal("2")),
            TicketLeg("event-1", "winner", " alice", Decimal("2")),
            TicketLeg("event|1", "winner", "alice", Decimal("2")),
            TicketLeg("event-1", "win|ner", "alice", Decimal("2")),
            TicketLeg("event-1", "winner", "ali|ce", Decimal("2")),
        )
        for leg in invalid_legs:
            with self.subTest(leg=leg):
                book = PaperBook("100")
                with self.assertRaises(ValueError):
                    book.open_ticket([leg], "10")
                self.assertEqual(book.balance, Decimal("100"))
                self.assertEqual(book.tickets, {})

    def test_open_ticket_rejects_noncanonical_leg_object_before_bankroll_mutation(self) -> None:
        book = PaperBook("100")

        with self.assertRaisesRegex(ValueError, "canonical TicketLeg"):
            book.open_ticket([object()], "10")

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_load_rejects_noncanonical_or_noninjective_leg_identity(self) -> None:
        cases = (
            ("", "winner", "alice", "non-empty trimmed string"),
            ("event-1", " winner", "alice", "non-empty trimmed string"),
            ("event-1", "winner", "alice ", "non-empty trimmed string"),
            ("event|1", "winner", "alice", "quote-key delimiter"),
            ("event-1", "win|ner", "alice", "quote-key delimiter"),
            ("event-1", "winner", "ali|ce", "quote-key delimiter"),
        )
        for event_id, market_id, selection_id, message in cases:
            with self.subTest(
                event_id=event_id,
                market_id=market_id,
                selection_id=selection_id,
            ):
                self._write_snapshot(
                    event_id=event_id,
                    market_id=market_id,
                    selection_id=selection_id,
                )
                with self.assertRaisesRegex(ValueError, message):
                    PaperBook.load(self.path)

    def test_save_revalidates_mutable_leg_identity_and_preserves_last_good_snapshot(self) -> None:
        book, ticket = self._valid_book()
        book.save(self.path)
        last_good = self.path.read_bytes()

        ticket.legs = (TicketLeg("event|alias", "winner", "alice", Decimal("2")),)

        with self.assertRaisesRegex(ValueError, "quote-key delimiter"):
            book.save(self.path)

        self.assertEqual(self.path.read_bytes(), last_good)
        restored = PaperBook.load(self.path)
        self.assertEqual(restored.balance, Decimal("90"))
        self.assertEqual(len(restored.tickets), 1)

    def test_save_rejects_mutated_ticket_mapping_identity_without_replacing_snapshot(self) -> None:
        book, ticket = self._valid_book()
        book.save(self.path)
        last_good = self.path.read_bytes()
        book.tickets["alias-ticket-id"] = book.tickets.pop(ticket.ticket_id)

        with self.assertRaisesRegex(ValueError, "mapping key must match ticket_id"):
            book.save(self.path)

        self.assertEqual(self.path.read_bytes(), last_good)

    def test_save_rejects_mutated_economic_state_without_replacing_snapshot(self) -> None:
        book, _ = self._valid_book()
        book.save(self.path)
        last_good = self.path.read_bytes()
        book.balance = Decimal("99")

        with self.assertRaisesRegex(ValueError, "balance is inconsistent"):
            book.save(self.path)

        self.assertEqual(self.path.read_bytes(), last_good)


if __name__ == "__main__":
    unittest.main()
