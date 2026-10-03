from __future__ import annotations

from autosport.the_odds_api_provider import (
    HttpJsonResponse,
    TheOddsApiProvider,
)


def _event(event_id: str, sport: str) -> dict[str, object]:
    return {
        "id": event_id,
        "sport_key": sport,
        "commence_time": "2026-09-29T20:00:00Z",
        "bookmakers": [
            {
                "key": "same-book",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-09-29T16:00:00Z",
                        "outcomes": [
                            {"name": "Draw", "price": "3.20"},
                        ],
                    }
                ],
            }
        ],
    }


def _transport(payload: list[dict[str, object]]):
    def read(_url: str, _timeout: float) -> HttpJsonResponse:
        return HttpJsonResponse(payload, 200, {})

    return read


def test_same_shape_markets_on_distinct_events_have_distinct_identities() -> None:
    provider = TheOddsApiProvider(
        "secret",
        sport="soccer_epl",
        transport=_transport(
            [
                _event("event-a", "soccer_epl"),
                _event("event-b", "soccer_epl"),
            ]
        ),
        clock=lambda: "2026-09-29T16:01:00Z",
    )

    quotes = provider.read_batch().quotes

    assert len(quotes) == 2
    assert quotes[0].provider_event_id != quotes[1].provider_event_id
    assert quotes[0].provider_market_id != quotes[1].provider_market_id
    assert quotes[0].provider_selection_id != quotes[1].provider_selection_id


def test_upcoming_cross_sport_same_event_key_does_not_alias_market_identity() -> None:
    provider = TheOddsApiProvider(
        "secret",
        sport="upcoming",
        transport=_transport(
            [
                _event("shared-event-key", "soccer_epl"),
                _event("shared-event-key", "basketball_nba"),
            ]
        ),
        clock=lambda: "2026-09-29T16:01:00Z",
    )

    quotes = provider.read_batch().quotes

    assert len(quotes) == 2
    assert quotes[0].provider_event_id == quotes[1].provider_event_id
    assert quotes[0].sport != quotes[1].sport
    assert quotes[0].provider_market_id != quotes[1].provider_market_id
    assert quotes[0].provider_selection_id != quotes[1].provider_selection_id
