from __future__ import annotations

import bz2
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.betfair_available_back import BetfairAvailableBackBook
from autosport.betfair_historical_import import import_betfair_historical


def _epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class BetfairAvailableBackNumericTypeIntegrityTests(unittest.TestCase):
    def test_decoder_rejects_coercible_text_for_ladder_numbers(self) -> None:
        bad_changes = (
            {"atb": [["2.0", 10.0]]},
            {"atb": [[2.0, "10.0"]]},
            {"batb": [[0, "2.0", 10.0]]},
            {"batb": [[0, 2.0, "10.0"]]},
            {"batb": [[0, "0", 0.0]]},
        )
        for change in bad_changes:
            with self.subTest(change=change):
                book = BetfairAvailableBackBook()
                with self.assertRaisesRegex(ValueError, "must be a finite number"):
                    book.apply(change, image=True)
                self.assertIsNone(book.current_quote())

    def test_rejected_batb_removal_is_transactional_but_numeric_zero_sentinel_still_removes(self) -> None:
        book = BetfairAvailableBackBook()
        book.apply({"batb": [[0, 2.0, 10.0]]}, image=True)
        before = book.current_quote()
        self.assertIsNotNone(before)

        with self.assertRaisesRegex(ValueError, "batb\[0\]\.price must be a finite number"):
            book.apply({"batb": [[0, "0", 0.0]]})
        self.assertEqual(book.current_quote(), before)

        with self.assertRaisesRegex(ValueError, "batb\[0\]\.price must be non-negative"):
            book.apply({"batb": [[0, -1.0, 0.0]]})
        self.assertEqual(book.current_quote(), before)

        update = book.apply({"batb": [[0, 0.0, 0.0]]})
        self.assertTrue(update.touched)
        self.assertIsNone(update.quote)
        self.assertIsNone(book.current_quote())

    def test_raw_historical_json_string_price_cannot_become_executable_quote_evidence(self) -> None:
        message = {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:00:00Z"),
            "mc": [
                {
                    "id": "1.numeric-type",
                    "img": True,
                    "marketDefinition": {
                        "eventId": "event-numeric-type",
                        "eventTypeId": "2593174",
                        "marketType": "MATCH_ODDS",
                        "status": "OPEN",
                        "eventName": "Player A v Player B",
                        "inPlay": False,
                        "betDelay": 0,
                        "priceLadderDefinition": {"type": "CLASSIC"},
                        "runners": [{"id": 101, "name": "Player A", "status": "ACTIVE"}],
                    },
                    "rc": [{"id": 101, "atb": [["1.9", 40.0]]}],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.bz2"
            with bz2.open(source, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(message) + "\n")

            with self.assertRaisesRegex(
                ValueError,
                r"invalid Betfair available-back ladder: atb\[0\]\.price must be a finite number",
            ):
                import_betfair_historical(
                    [source],
                    root / "dataset",
                    acquired_at="2026-02-10T13:30:00Z",
                    imported_at="2026-02-10T14:00:00Z",
                    terms_reference="test-rights",
                    retention_basis="test-retention",
                )


if __name__ == "__main__":
    unittest.main()
