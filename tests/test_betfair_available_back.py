from __future__ import annotations

import unittest
from decimal import Decimal

from autosport.betfair_available_back import BetfairAvailableBackBook


class BetfairAvailableBackBookTests(unittest.TestCase):
    def test_pro_atb_requires_image_before_quote_is_verified(self) -> None:
        book = BetfairAvailableBackBook()

        preimage = book.apply({"atb": [[2.0, 12.0]]})
        self.assertTrue(preimage.touched)
        self.assertEqual(preimage.quote.decimal_odds, Decimal("2.0"))
        self.assertFalse(preimage.quote.cache_verified)

        image = book.apply({"atb": [[1.9, 5.0], [2.0, 10.0]]}, image=True)
        self.assertEqual(image.quote.decimal_odds, Decimal("2.0"))
        self.assertEqual(image.quote.available_size, Decimal("10.0"))
        self.assertTrue(image.quote.cache_verified)
        self.assertEqual(image.quote.provider_price_field, "rc[].atb")

        delta = book.apply({"atb": [[2.0, 0], [1.95, 8.0]]})
        self.assertEqual(delta.quote.decimal_odds, Decimal("1.95"))
        self.assertEqual(delta.quote.available_size, Decimal("8.0"))
        self.assertTrue(delta.quote.cache_verified)

    def test_advanced_batb_is_keyed_by_level_and_never_promotes_deeper_level(self) -> None:
        book = BetfairAvailableBackBook()
        image = book.apply(
            {"batb": [[0, 2.1, 9.0], [1, 2.08, 11.0], [2, 2.06, 20.0]]},
            image=True,
        )
        self.assertEqual(image.quote.decimal_odds, Decimal("2.1"))
        self.assertEqual(image.quote.available_size, Decimal("9.0"))
        self.assertTrue(image.quote.cache_verified)
        self.assertEqual(image.quote.provider_price_field, "rc[].batb")

        removed = book.apply({"batb": [[0, 0, 0]]})
        self.assertIsNone(removed.quote)

        restored = book.apply({"batb": [[0, 2.04, 7.0]]})
        self.assertEqual(restored.quote.decimal_odds, Decimal("2.04"))
        self.assertEqual(restored.quote.available_size, Decimal("7.0"))

    def test_image_resets_ladder_and_allows_encoding_change(self) -> None:
        book = BetfairAvailableBackBook()
        book.apply({"batb": [[0, 2.0, 5.0]]}, image=True)

        with self.assertRaisesRegex(ValueError, "encoding changed"):
            book.apply({"atb": [[2.02, 6.0]]})

        switched = book.apply({"atb": [[2.02, 6.0]]}, image=True)
        self.assertEqual(switched.quote.decimal_odds, Decimal("2.02"))
        self.assertEqual(switched.quote.ladder_kind, "full_price_ladder")

    def test_malformed_or_nonfinite_ladder_values_fail_closed(self) -> None:
        bad_changes = (
            {"atb": [[2.0, float("nan")]]},
            {"atb": [[float("inf"), 1.0]]},
            {"batb": [[-1, 2.0, 1.0]]},
            {"batb": [[0, 1.0, 1.0]]},
            {"atb": [[2.0, -1.0]]},
            {"atb": [[2.0, 1.0]], "batb": [[0, 2.0, 1.0]]},
        )
        for change in bad_changes:
            with self.subTest(change=change):
                book = BetfairAvailableBackBook()
                with self.assertRaises(ValueError):
                    book.apply(change, image=True)


if __name__ == "__main__":
    unittest.main()
