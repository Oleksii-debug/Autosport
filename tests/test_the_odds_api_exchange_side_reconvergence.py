from decimal import Decimal

import pytest

from autosport.providers import CanonicalNormalizer
from autosport.the_odds_api_provider import HttpJsonResponse, TheOddsApiProvider


def _event(market_key: str) -> dict[str, object]:
    return {
        "id": "event-1",
        "sport_key": "soccer_epl",
        "commence_time": "2026-09-23T12:00:00Z",
        "bookmakers": [
            {
                "key": "book-a",
                "markets": [
                    {
                        "key": market_key,
                        "last_update": "2026-09-23T07:59:00Z",
                        "outcomes": [
                            {
                                "name": "Alpha",
                                "price": Decimal("2.10"),
                            }
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    ("requested_market", "returned_market"),
    (("h2h", "h2h_lay"), ("outrights", "outrights_lay")),
)
def test_lay_market_propagates_typed_exchange_side_into_canonical_event(
    requested_market: str,
    returned_market: str,
) -> None:
    provider = TheOddsApiProvider(
        "test-key",
        sport="soccer_epl",
        markets=(requested_market,),
        transport=lambda *_: HttpJsonResponse([_event(returned_market)], 200, {}),
        clock=lambda: "2026-09-23T08:00:00+00:00",
    )

    quote = provider.read_batch().quotes[0]
    event = CanonicalNormalizer().normalize(provider.source_id, quote)

    assert quote.metadata["exchange_side"] == "lay"
    assert quote.exchange_side == "lay"
    assert event.exchange_side == "lay"


def test_back_market_does_not_fabricate_typed_exchange_side() -> None:
    provider = TheOddsApiProvider(
        "test-key",
        sport="soccer_epl",
        markets=("h2h",),
        transport=lambda *_: HttpJsonResponse([_event("h2h")], 200, {}),
        clock=lambda: "2026-09-23T08:00:00+00:00",
    )

    quote = provider.read_batch().quotes[0]
    event = CanonicalNormalizer().normalize(provider.source_id, quote)

    assert quote.metadata["exchange_side"] is None
    assert quote.exchange_side is None
    assert event.exchange_side is None
