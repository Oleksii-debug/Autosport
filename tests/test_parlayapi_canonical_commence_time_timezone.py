from __future__ import annotations

import copy

import pytest

from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


BASE_EVENT = {
    "id": "tt-100",
    "sport_key": "table_tennis",
    "commence_time": "2026-09-12T20:30:00Z",
    "home_team": "Player A",
    "away_team": "Player B",
    "bookmakers": [
        {
            "key": "book-a",
            "title": "Book A",
            "last_update": "2026-09-12T20:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-09-12T20:00:01Z",
                    "outcomes": [{"name": "Player A", "price": 1.80}],
                }
            ],
        }
    ],
}


def _read(event: dict):
    provider = ParlayApiTableTennisProvider(
        "key",
        transport=lambda *_: HttpJsonResponse([event], 200, {}),
        clock=lambda: "2026-09-12T20:00:10+00:00",
    )
    return provider.read_batch().quotes


def test_timezone_naive_commence_time_cannot_be_canonical_identity_witness():
    event = copy.deepcopy(BASE_EVENT)
    event["commence_time"] = "2026-09-12T20:30:00"

    with pytest.raises(ProviderPayloadError):
        _read(event)
