import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg, TicketStatus
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

    def _write_snapshot(
        self,
        *,
        event_id: str,
        market_id: str,
        selection_id: str,
        placed_at: object = "2026-09-14T00:00:00+00:00",
    ) -> None:
        payload = {
            "initial_bankroll": "100",
            "balance": "90",
            "tickets": [
                {
                    "ticket_id": "ticket-1",
                    "stake": "10",
                    "placed_at": placed_at,
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

    def test_open_ticket_rejects_unsaveable_metadata_before_bankroll_mutation(self) -> None:
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        cases = (
            {"placed_at": " "},
            {"placed_at": " 2026-09-14T00:00:00+00:00"},
            {"placed_at": "2026-09-14T00:00:00"},
            {"placed_at": 0},
            {"reason": None},
            {"reason": object()},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                book = PaperBook("100")
                with self.assertRaises(ValueError):
                    book.open_ticket([leg], "10", **kwargs)
                self.assertEqual(book.balance, Decimal("100"))
                self.assertEqual(book.tickets, {})

    def test_open_ticket_preserves_explicit_timezone_aware_timestamp(self) -> None:
        book = PaperBook("100")
        placed_at = "2026-09-14T09:00:00Z"

        ticket = book.open_ticket(
            [TicketLeg("event-1", "winner", "alice", Decimal("2"))],
            "10",
            placed_at=placed_at,
        )

        self.assertEqual(ticket.placed_at, placed_at)
        self.assertEqual(book.balance, Decimal("90"))

    def test_load_rejects_invalid_persisted_timestamp(self) -> None:
        for placed_at in ("", "2026-09-14T09:00:00", 0):
            with self.subTest(placed_at=placed_at):
                self._write_snapshot(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="alice",
                    placed_at=placed_at,
                )

                with self.assertRaisesRegex(ValueError, "snapshot placed_at"):
                    PaperBook.load(self.path)

    def test_save_rejects_mutated_invalid_timestamp_without_replacing_snapshot(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            [TicketLeg("event-1", "winner", "alice", Decimal("2"))],
            "10",
            placed_at="2026-09-14T09:00:00+00:00",
        )
        book.save(self.path)
        last_good = self.path.read_bytes()
        ticket.placed_at = ""

        with self.assertRaisesRegex(ValueError, "snapshot placed_at"):
            book.save(self.path)

        self.assertEqual(self.path.read_bytes(), last_good)
        self.assertFalse(self.path.with_suffix(self.path.suffix + ".tmp").exists())

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

    def test_settlement_arithmetic_failure_does_not_partially_mutate_ticket_or_balance(self) -> None:
        book = PaperBook("9E+999999")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "4.5E+999999")
        before_balance = book.balance

        with self.assertRaisesRegex(ValueError, "settlement arithmetic is not representable"):
            book.settle(ticket.ticket_id, {leg.quote_key})

        self.assertEqual(book.balance, before_balance)
        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))

    def test_losing_ticket_does_not_evaluate_irrelevant_overflowing_win_product(self) -> None:
        book = PaperBook("100")
        legs = (
            TicketLeg("event-1", "winner", "a", Decimal("9E+999999")),
            TicketLeg("event-2", "winner", "b", Decimal("2")),
            TicketLeg("event-3", "winner", "c", Decimal("2")),
        )
        ticket = book.open_ticket(legs, "10")

        settled = book.settle(ticket.ticket_id, {legs[0].quote_key, legs[1].quote_key})

        self.assertIs(settled.status, TicketStatus.LOST)
        self.assertEqual(settled.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))

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
