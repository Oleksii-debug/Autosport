from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class ProviderAccountPaperProvenanceTests(unittest.TestCase):
    @staticmethod
    def _leg() -> TicketLeg:
        return TicketLeg("event-1", "market-1", "selection-1", Decimal("2"))

    def test_provider_account_identity_survives_save_restart_exactly(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            [self._leg()],
            Decimal("10"),
            placed_at="2026-09-18T00:00:00+00:00",
            provider_source_ids=("provider-1",),
            provider_accounts=(("provider-1", "account-A"),),
            bankroll_id="paper-bankroll",
            currency="USD",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            book.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            restarted = PaperBook.load(path)

        self.assertEqual(payload["schema_version"], 6)
        self.assertEqual(
            payload["tickets"][0]["provider_accounts"],
            [{"source_id": "provider-1", "account_id": "account-A"}],
        )
        self.assertEqual(
            restarted.tickets[ticket.ticket_id].provider_accounts,
            (("provider-1", "account-A"),),
        )

    def test_provider_account_identity_rejects_incomplete_or_ambiguous_binding(self) -> None:
        book = PaperBook("100")
        with self.assertRaisesRegex(
            ValueError,
            "provider_accounts must cover provider_source_ids exactly",
        ):
            book.open_ticket(
                [self._leg()],
                Decimal("10"),
                provider_source_ids=("provider-1",),
                provider_accounts=(("provider-2", "account-A"),),
                bankroll_id="paper-bankroll",
                currency="USD",
            )

        with self.assertRaisesRegex(
            ValueError,
            "at most one account_id per provider source",
        ):
            book.open_ticket(
                [self._leg()],
                Decimal("10"),
                provider_source_ids=("provider-1",),
                provider_accounts=(
                    ("provider-1", "account-A"),
                    ("provider-1", "account-B"),
                ),
                bankroll_id="paper-bankroll",
                currency="USD",
            )

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_schema3_remains_backward_readable_with_unknown_account_provenance(self) -> None:
        raw = {
            "schema_version": 3,
            "initial_bankroll": "100",
            "balance": "90",
            "tickets": [
                {
                    "ticket_id": "legacy-v3",
                    "stake": "10",
                    "placed_at": "2026-09-18T00:00:00+00:00",
                    "status": "open",
                    "payout": "0",
                    "strategy_reason": "",
                    "provider_source_ids": ["provider-1"],
                    "bankroll_id": "paper-bankroll",
                    "currency": "USD",
                    "legs": [
                        {
                            "event_id": "event-1",
                            "market_id": "market-1",
                            "selection_id": "selection-1",
                            "locked_odds": "2",
                        }
                    ],
                }
            ],
            "lifecycle": [{"action": "open", "ticket_id": "legacy-v3"}],
        }

        loaded = PaperBook.load_bytes(
            json.dumps(raw, ensure_ascii=False).encode("utf-8")
        )

        self.assertEqual(loaded.tickets["legacy-v3"].provider_accounts, ())
        self.assertEqual(loaded.tickets["legacy-v3"].provider_source_ids, ("provider-1",))


if __name__ == "__main__":
    unittest.main()
