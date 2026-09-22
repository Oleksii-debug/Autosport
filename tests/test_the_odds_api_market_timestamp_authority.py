from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.the_odds_api_reference import (
    TheOddsApiHttpJsonResponse,
    TheOddsApiPayloadError,
    TheOddsApiReferenceProvider,
)


_BODY_SHA = "b" * 64


def _response_without_market_timestamp() -> TheOddsApiHttpJsonResponse:
    payload = [
        {
            "id": "event-123",
            "sport_key": "basketball_nba",
            "sport_title": "NBA",
            "commence_time": "2026-09-22T08:00:00Z",
            "home_team": "Home Team",
            "away_team": "Away Team",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    # The provider documents bookmaker-level last_update as
                    # deprecated.  It is deliberately present so this regression
                    # proves it cannot substitute for market-level freshness.
                    "last_update": "2026-09-22T06:55:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {
                                    "name": "Home Team",
                                    "price": Decimal("1.90"),
                                },
                                {
                                    "name": "Away Team",
                                    "price": Decimal("2.05"),
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    return TheOddsApiHttpJsonResponse(
        payload=payload,
        status_code=200,
        headers={},
        body_sha256=_BODY_SHA,
    )


def test_deprecated_bookmaker_timestamp_cannot_mint_market_freshness() -> None:
    provider = TheOddsApiReferenceProvider(
        "synthetic-secret",
        sport_key="basketball_nba",
        markets=("h2h",),
        bookmakers=("draftkings",),
        transport=lambda *_args: _response_without_market_timestamp(),
        clock=lambda: "2026-09-22T07:00:00Z",
    )

    with pytest.raises(
        TheOddsApiPayloadError,
        match="market.*last_update|requires provider last_update timestamp",
    ):
        provider.read_batch()
