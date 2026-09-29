from __future__ import annotations

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class PaperBookSerializedIngressIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "paper_book.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "schema_version": 2,
            "initial_bankroll": "100",
            "balance": "90",
            "tickets": [
                {
                    "ticket_id": "ticket-1",
                    "stake": "10",
                    "placed_at": "2026-09-14T09:00:00+00:00",
                    "status": "open",
                    "payout": "0",
                    "strategy_reason": "canonical",
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
            ],
        }

    @staticmethod
    def _encoded(payload: dict[str, object]) -> bytes:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")

    @staticmethod
    def _leg() -> TicketLeg:
        return TicketLeg("event-1", "winner", "alice", Decimal("2"))

    def test_present_null_schema_version_is_not_treated_as_legacy(self) -> None:
        payload = self._payload()
        payload["schema_version"] = None

        with self.assertRaisesRegex(ValueError, "unsupported PaperBook snapshot schema_version"):
            PaperBook.load_bytes(self._encoded(payload))

    def test_schema2_rejects_non_string_decimal_forms_in_every_economic_field(self) -> None:
        mutations = (
            ("initial_bankroll", lambda payload: payload.__setitem__("initial_bankroll", 100)),
            ("balance", lambda payload: payload.__setitem__("balance", 90.0)),
            ("stake", lambda payload: payload["tickets"][0].__setitem__("stake", True)),
            ("payout", lambda payload: payload["tickets"][0].__setitem__("payout", False)),
            (
                "locked_odds",
                lambda payload: payload["tickets"][0]["legs"][0].__setitem__("locked_odds", 2),
            ),
        )

        for label, mutate in mutations:
            with self.subTest(label=label):
                payload = self._payload()
                mutate(payload)
                with self.assertRaisesRegex(ValueError, "decimal string"):
                    PaperBook.load_bytes(self._encoded(payload))

    def test_schema2_rejects_bool_numeric_list_and_object_decimal_coercions(self) -> None:
        for malformed in (True, 1, 1.25, [], {}):
            with self.subTest(malformed=repr(malformed)):
                payload = self._payload()
                payload["tickets"][0]["stake"] = malformed
                with self.assertRaisesRegex(ValueError, "decimal string"):
                    PaperBook.load_bytes(self._encoded(payload))

    def test_high_precision_decimal_strings_preserve_exact_identity(self) -> None:
        exact = "100.123456789012345678901234567890123456789"
        payload = {
            "schema_version": 2,
            "initial_bankroll": exact,
            "balance": exact,
            "tickets": [],
            "lifecycle": [],
        }

        book = PaperBook.load_bytes(self._encoded(payload))

        self.assertEqual(book.initial_bankroll, Decimal(exact))
        self.assertEqual(book.balance, Decimal(exact))
        self.assertEqual(str(book.initial_bankroll), exact)
        self.assertEqual(str(book.balance), exact)

    def test_open_ticket_rejects_lone_surrogate_reason_before_mutation(self) -> None:
        book = PaperBook("100")
        before_balance = book.balance

        with self.assertRaisesRegex(ValueError, "valid UTF-8 text"):
            book.open_ticket(
                [self._leg()],
                "10",
                reason="\ud800",
                placed_at="2026-09-14T09:00:00+00:00",
            )

        self.assertEqual(book.balance, before_balance)
        self.assertEqual(book.tickets, {})
        self.assertEqual(book._lifecycle, [])

    def test_load_bytes_rejects_ascii_escaped_lone_surrogate_text(self) -> None:
        payload = self._payload()
        payload["tickets"][0]["strategy_reason"] = "\ud800"
        encoded = self._encoded(payload)
        self.assertTrue(encoded.isascii())
        self.assertIn(b"\\ud800", encoded)

        with self.assertRaisesRegex(ValueError, "valid UTF-8 text"):
            PaperBook.load_bytes(encoded)

    def test_lifecycle_keys_must_be_utf8_round_trip_safe(self) -> None:
        payload = self._payload()
        payload["lifecycle"][0]["ticket_id"] = "\ud800"

        with self.assertRaisesRegex(ValueError, "valid UTF-8 text"):
            PaperBook.load_bytes(self._encoded(payload))

    def test_save_rejects_mutated_lone_surrogate_before_creating_tmp_file(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            [self._leg()],
            "10",
            reason="safe",
            placed_at="2026-09-14T09:00:00+00:00",
        )
        ticket.strategy_reason = "\ud800"
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")

        with self.assertRaisesRegex(ValueError, "valid UTF-8 text"):
            book.save(self.path)

        self.assertFalse(self.path.exists())
        self.assertFalse(temporary.exists())

    def test_snapshot_leg_identity_rejects_lone_surrogate(self) -> None:
        payload = copy.deepcopy(self._payload())
        payload["tickets"][0]["legs"][0]["selection_id"] = "\ud800"

        with self.assertRaisesRegex(ValueError, "valid UTF-8 text"):
            PaperBook.load_bytes(self._encoded(payload))

    def test_load_bytes_normalizes_json_parser_recursion_exhaustion(self) -> None:
        deeply_nested_json = ("[" * 10000 + "]" * 10000).encode("ascii")

        with self.assertRaisesRegex(ValueError, "JSON nesting is too deep"):
            PaperBook.load_bytes(deeply_nested_json)

    def test_load_bytes_rejects_missing_required_root_and_ticket_fields(self) -> None:
        root_missing = self._payload()
        root_missing.pop("initial_bankroll")
        with self.assertRaisesRegex(ValueError, "missing required field: initial_bankroll"):
            PaperBook.load_bytes(self._encoded(root_missing))

        ticket_missing = self._payload()
        ticket_missing["tickets"][0].pop("status")
        with self.assertRaisesRegex(ValueError, "missing required field: status"):
            PaperBook.load_bytes(self._encoded(ticket_missing))

    def test_load_bytes_rejects_non_object_leg_member(self) -> None:
        payload = self._payload()
        payload["tickets"][0]["legs"] = [1]

        with self.assertRaisesRegex(ValueError, "leg 0.*must be an object"):
            PaperBook.load_bytes(self._encoded(payload))

    def test_load_bytes_rejects_non_list_legs_container(self) -> None:
        for malformed in (None, 1, {}, "not-a-list"):
            with self.subTest(malformed=repr(malformed)):
                payload = self._payload()
                payload["tickets"][0]["legs"] = malformed
                with self.assertRaisesRegex(ValueError, "legs for ticket ticket-1 must be a list"):
                    PaperBook.load_bytes(self._encoded(payload))

    def test_load_bytes_accepts_canonical_schema2_control_after_shape_validation(self) -> None:
        book = PaperBook.load_bytes(self._encoded(self._payload()))

        self.assertEqual(book.initial_bankroll, Decimal("100"))
        self.assertEqual(book.balance, Decimal("90"))
        self.assertEqual(tuple(book.tickets), ("ticket-1",))
        self.assertEqual(book._lifecycle, [("open", "ticket-1", (), ())])


if __name__ == "__main__":
    unittest.main()
