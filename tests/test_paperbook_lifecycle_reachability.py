import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook


class PaperBookLifecycleReachabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "paper_book.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _leg(event_id: str, selection_id: str, odds: str) -> TicketLeg:
        return TicketLeg(event_id, "winner", selection_id, Decimal(odds))

    def test_partial_void_won_round_trip_persists_exact_settlement_witness(self) -> None:
        book = PaperBook("100")
        first = self._leg("event-1", "alice", "2")
        second = self._leg("event-2", "bob", "3")
        ticket = book.open_ticket(
            [first, second],
            "10",
            placed_at="2026-09-14T09:00:00+00:00",
        )

        book.settle(
            ticket.ticket_id,
            {first.quote_key},
            {second.quote_key},
        )
        book.save(self.path)

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 4)
        self.assertEqual(
            payload["lifecycle"],
            [
                {"action": "open", "ticket_id": ticket.ticket_id},
                {
                    "action": "settle",
                    "ticket_id": ticket.ticket_id,
                    "winning_quote_keys": [first.quote_key],
                    "void_quote_keys": [second.quote_key],
                },
            ],
        )

        restored = PaperBook.load_bytes(self.path.read_bytes())
        restored_ticket = restored.tickets[ticket.ticket_id]
        self.assertIs(restored_ticket.status, TicketStatus.WON)
        self.assertEqual(restored_ticket.payout, Decimal("20"))
        self.assertEqual(restored.balance, Decimal("110"))

    def test_schema2_rejects_economically_unreachable_won_payout(self) -> None:
        payload = {
            "schema_version": 2,
            "initial_bankroll": "100",
            "balance": "1089",
            "tickets": [
                {
                    "ticket_id": "ticket-1",
                    "stake": "10",
                    "placed_at": "2026-09-14T09:00:00+00:00",
                    "status": "won",
                    "payout": "999",
                    "strategy_reason": "forged",
                    "legs": [
                        {
                            "event_id": "event-1",
                            "market_id": "winner",
                            "selection_id": "alice",
                            "locked_odds": "2",
                        }
                    ],
                }
            ],
            "lifecycle": [
                {"action": "open", "ticket_id": "ticket-1"},
                {
                    "action": "settle",
                    "ticket_id": "ticket-1",
                    "winning_quote_keys": ["event-1|winner|alice"],
                    "void_quote_keys": [],
                },
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "inconsistent with lifecycle settlement witness"):
            PaperBook.load(self.path)

    def test_schema2_rejects_ticket_that_could_not_be_afforded_when_opened(self) -> None:
        payload = {
            "schema_version": 2,
            "initial_bankroll": "100",
            "balance": "250",
            "tickets": [
                {
                    "ticket_id": "ticket-1",
                    "stake": "150",
                    "placed_at": "2026-09-14T09:00:00+00:00",
                    "status": "won",
                    "payout": "300",
                    "strategy_reason": "self-financing forgery",
                    "legs": [
                        {
                            "event_id": "event-1",
                            "market_id": "winner",
                            "selection_id": "alice",
                            "locked_odds": "2",
                        }
                    ],
                }
            ],
            "lifecycle": [
                {"action": "open", "ticket_id": "ticket-1"},
                {
                    "action": "settle",
                    "ticket_id": "ticket-1",
                    "winning_quote_keys": ["event-1|winner|alice"],
                    "void_quote_keys": [],
                },
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "was not affordable"):
            PaperBook.load(self.path)

    def test_settlement_rejects_unknown_resolution_key_before_mutation(self) -> None:
        book = PaperBook("100")
        leg = self._leg("event-1", "alice", "2")
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-14T09:00:00+00:00",
        )
        before_balance = book.balance

        with self.assertRaisesRegex(ValueError, "unknown winning quote_key"):
            book.settle(ticket.ticket_id, {leg.quote_key, "foreign|market|selection"})

        self.assertEqual(book.balance, before_balance)
        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))

    def test_winning_payout_that_rounds_to_stake_fails_atomically(self) -> None:
        book = PaperBook("100")
        leg = self._leg(
            "event-1",
            "alice",
            "1.00000000000000000000000000000000000000001",
        )
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-14T09:00:00+00:00",
        )
        before_balance = book.balance

        with self.assertRaisesRegex(
            ValueError,
            "winning settlement payout must exceed stake",
        ):
            book.settle(ticket.ticket_id, {leg.quote_key})

        self.assertEqual(book.balance, before_balance)
        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))

    def test_legacy_open_only_snapshot_remains_loadable_and_upgrades_on_save(self) -> None:
        payload = {
            "initial_bankroll": "100",
            "balance": "90",
            "tickets": [
                {
                    "ticket_id": "ticket-1",
                    "stake": "10",
                    "placed_at": "2026-09-14T09:00:00+00:00",
                    "status": "open",
                    "payout": "0",
                    "strategy_reason": "legacy",
                    "legs": [
                        {
                            "event_id": "event-1",
                            "market_id": "winner",
                            "selection_id": "alice",
                            "locked_odds": "2",
                        }
                    ],
                }
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        book = PaperBook.load(self.path)
        book.save(self.path)
        upgraded = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(upgraded["schema_version"], 4)
        self.assertEqual(
            upgraded["lifecycle"],
            [{"action": "open", "ticket_id": "ticket-1"}],
        )

    def test_legacy_settled_snapshot_fails_closed_without_lifecycle_provenance(self) -> None:
        payload = {
            "initial_bankroll": "100",
            "balance": "110",
            "tickets": [
                {
                    "ticket_id": "ticket-1",
                    "stake": "10",
                    "placed_at": "2026-09-14T09:00:00+00:00",
                    "status": "won",
                    "payout": "20",
                    "strategy_reason": "legacy",
                    "legs": [
                        {
                            "event_id": "event-1",
                            "market_id": "winner",
                            "selection_id": "alice",
                            "locked_odds": "2",
                        }
                    ],
                }
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "missing lifecycle provenance"):
            PaperBook.load(self.path)


if __name__ == "__main__":
    unittest.main()
