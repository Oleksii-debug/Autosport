from __future__ import annotations

from decimal import Decimal

from autosport.providers import CanonicalNormalizer
from autosport.the_odds_api_reference import (
    TheOddsApiHttpJsonResponse,
    TheOddsApiReferenceProvider,
)


REQUEST_STARTED = "2026-09-22T06:00:00Z"
RESPONSE_AVAILABLE = "2026-09-22T06:00:05Z"
BODY_SHA = "c" * 64


def _event_payload() -> list[dict]:
    return [
        {
            "id": "event-causal-1",
            "sport_key": "basketball_nba",
            "sport_title": "NBA",
            "commence_time": "2026-09-22T08:00:00Z",
            "home_team": "Home",
            "away_team": "Away",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "last_update": "2026-09-22T05:59:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-09-22T05:59:30Z",
                            "outcomes": [
                                {"name": "Home", "price": Decimal("1.90")},
                                {"name": "Away", "price": Decimal("2.05")},
                            ],
                        }
                    ],
                }
            ],
        }
    ]


def _provider(*, historical: bool) -> TheOddsApiReferenceProvider:
    transport_returned = {"value": False}

    def clock() -> str:
        return RESPONSE_AVAILABLE if transport_returned["value"] else REQUEST_STARTED

    def transport(path, params, api_key, timeout):
        transport_returned["value"] = True
        payload: object = _event_payload()
        if historical:
            payload = {
                "timestamp": "2026-09-22T05:55:00Z",
                "previous_timestamp": "2026-09-22T05:50:00Z",
                "next_timestamp": "2026-09-22T06:00:00Z",
                "data": _event_payload(),
            }
        return TheOddsApiHttpJsonResponse(payload, 200, {}, BODY_SHA)

    return TheOddsApiReferenceProvider(
        "test-secret",
        sport_key="basketball_nba",
        markets=("h2h",),
        bookmakers=("draftkings",),
        historical_at="2026-09-22T06:00:00Z" if historical else None,
        transport=transport,
        clock=clock,
    )


def _assert_response_completion_is_causal_availability(
    provider: TheOddsApiReferenceProvider,
) -> None:
    batch = provider.read_batch()
    quote = batch.quotes[0]

    assert quote.source_ts == "2026-09-22T05:59:30Z"
    assert quote.observed_ts == RESPONSE_AVAILABLE
    assert quote.metadata["acquired_at"] == RESPONSE_AVAILABLE

    normalized = CanonicalNormalizer().normalize(batch.source_id, quote)
    assert normalized.observed_ts == RESPONSE_AVAILABLE
    assert normalized.ingest_ts == RESPONSE_AVAILABLE


def test_current_response_cannot_be_backdated_to_pre_transport_clock_sample() -> None:
    _assert_response_completion_is_causal_availability(_provider(historical=False))


def test_historical_response_cannot_be_backdated_to_pre_transport_clock_sample() -> None:
    _assert_response_completion_is_causal_availability(_provider(historical=True))
