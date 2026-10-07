from __future__ import annotations

import bz2
import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from autosport.betfair_available_back import BetfairAvailableBackBook
from autosport.betfair_historical_import import import_betfair_historical


def _epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class BetfairAdvancedLevelBoundsTests(unittest.TestCase):
    def test_best_three_accepts_only_provider_levels_zero_through_two(self) -> None:
        book = BetfairAvailableBackBook()
        update = book.apply(
            {
                "batb": [
                    [0, 2.0, 10.0],
                    [1, 1.99, 8.0],
                    [2, 1.98, 6.0],
                ]
            },
            image=True,
        )

        self.assertTrue(update.touched)
        self.assertIsNotNone(update.quote)
        assert update.quote is not None
        self.assertEqual(update.quote.decimal_odds, Decimal("2.0"))
        self.assertEqual(update.quote.available_size, Decimal("10.0"))
        self.assertTrue(update.quote.cache_verified)

        before = book.current_quote()
        with self.assertRaisesRegex(
            ValueError,
            r"batb\[0\]\.level must be an integer between 0 and 2",
        ):
            book.apply({"batb": [[3, 1.97, 4.0]]})
        self.assertEqual(book.current_quote(), before)

    def test_invalid_deeper_level_rolls_back_entire_provider_message(self) -> None:
        book = BetfairAvailableBackBook()

        with self.assertRaisesRegex(
            ValueError,
            r"batb\[1\]\.level must be an integer between 0 and 2",
        ):
            book.apply(
                {
                    "batb": [
                        [0, 2.0, 10.0],
                        [3, 1.97, 4.0],
                    ]
                },
                image=True,
            )

        self.assertIsNone(book.current_quote())
        self.assertIsNone(book.mode)

    def test_historical_import_rejects_advanced_level_outside_best_three(self) -> None:
        message = {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:00:00Z"),
            "mc": [
                {
                    "id": "1.advanced-level-bound",
                    "img": True,
                    "marketDefinition": {
                        "eventId": "event-advanced-level-bound",
                        "eventTypeId": "2593174",
                        "marketType": "MATCH_ODDS",
                        "status": "OPEN",
                        "eventName": "Player A v Player B",
                        "inPlay": False,
                        "betDelay": 0,
                        "priceLadderDefinition": {"type": "CLASSIC"},
                        "runners": [
                            {"id": 101, "name": "Player A", "status": "ACTIVE"}
                        ],
                    },
                    "rc": [{"id": 101, "batb": [[3, 1.9, 40.0]]}],
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
                r"invalid Betfair available-back ladder: "
                r"batb\[0\]\.level must be an integer between 0 and 2",
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
