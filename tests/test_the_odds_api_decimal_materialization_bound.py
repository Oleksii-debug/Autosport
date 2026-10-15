import unittest
from decimal import Decimal

import autosport.the_odds_api_provider as odds_api_module
from autosport.the_odds_api_provider import (
    HttpJsonResponse,
    TheOddsApiPayloadError,
    TheOddsApiProvider,
)


def _event(
    *,
    market_key="h2h",
    price=Decimal("2.10"),
    point=None,
    bet_limit=None,
):
    outcome = {"name": "Alpha", "price": price}
    if point is not None:
        outcome["point"] = point
    if bet_limit is not None:
        outcome["bet_limit"] = bet_limit
    return {
        "id": "0123456789abcdef0123456789abcdef",
        "sport_key": "soccer_epl",
        "commence_time": "2026-09-23T12:00:00Z",
        "bookmakers": [
            {
                "key": "book-a",
                "markets": [
                    {
                        "key": market_key,
                        "last_update": "2026-09-23T09:59:59Z",
                        "outcomes": [outcome],
                    }
                ],
            }
        ],
    }


def _provider(payload, *, market="h2h", include_bet_limits=False):
    return TheOddsApiProvider(
        "test-key",
        sport="soccer_epl",
        markets=(market,),
        include_bet_limits=include_bet_limits,
        transport=lambda *_: HttpJsonResponse([payload], 200, {}),
        clock=lambda: "2026-09-23T10:00:00+00:00",
    )


class TheOddsApiDecimalMaterializationBoundTests(unittest.TestCase):
    def test_fixed_point_boundary_is_exact(self):
        positive_at_limit = odds_api_module._decimal_text(Decimal("1E+4095"))
        fractional_at_limit = odds_api_module._decimal_text(Decimal("1E-4094"))

        self.assertEqual(len(positive_at_limit), 4096)
        self.assertEqual(len(fractional_at_limit), 4096)

        for value in (Decimal("1E+4096"), Decimal("1E-4095")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    TheOddsApiPayloadError,
                    "fixed-point representation exceeds bounded size",
                ):
                    odds_api_module._decimal_text(value)

    def test_compact_huge_price_fails_before_quote_identity_materialization(self):
        provider = _provider(_event(price=Decimal("1E+1000000")))

        with self.assertRaisesRegex(
            TheOddsApiPayloadError,
            "fixed-point representation exceeds bounded size",
        ):
            provider.read_batch()

    def test_compact_huge_point_fails_before_market_identity_materialization(self):
        provider = _provider(
            _event(
                market_key="spreads",
                point=Decimal("1E-1000000"),
            ),
            market="spreads",
        )

        with self.assertRaisesRegex(
            TheOddsApiPayloadError,
            "fixed-point representation exceeds bounded size",
        ):
            provider.read_batch()

    def test_compact_huge_bet_limit_fails_before_metadata_materialization(self):
        provider = _provider(
            _event(bet_limit=Decimal("1E+1000000")),
            include_bet_limits=True,
        )

        with self.assertRaisesRegex(
            TheOddsApiPayloadError,
            "fixed-point representation exceeds bounded size",
        ):
            provider.read_batch()

    def test_ordinary_exact_decimal_scale_is_preserved(self):
        provider = _provider(
            _event(
                market_key="spreads",
                price=Decimal("2.1000"),
                point=Decimal("-1.50"),
                bet_limit=Decimal("123.4500"),
            ),
            market="spreads",
            include_bet_limits=True,
        )

        quote = provider.read_batch().quotes[0]

        self.assertEqual(quote.decimal_odds, Decimal("2.1000"))
        self.assertEqual(quote.metadata["point"], "-1.50")
        self.assertEqual(quote.metadata["bet_limit"], "123.4500")


if __name__ == "__main__":
    unittest.main()
