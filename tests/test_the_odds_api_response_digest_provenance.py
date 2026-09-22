from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.the_odds_api_reference import (
    TheOddsApiHttpJsonResponse,
    TheOddsApiPayloadError,
    TheOddsApiReferenceProvider,
)


def _payload() -> list[dict]:
    return [
        {
            "id": "event-digest-1",
            "sport_key": "basketball_nba",
            "commence_time": "2026-09-22T08:00:00Z",
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


def test_successful_reference_response_requires_exact_body_digest() -> None:
    provider = TheOddsApiReferenceProvider(
        "test-secret",
        sport_key="basketball_nba",
        markets=("h2h",),
        bookmakers=("draftkings",),
        transport=lambda *_: TheOddsApiHttpJsonResponse(_payload(), 200, {}),
        clock=lambda: "2026-09-22T06:00:00Z",
    )

    with pytest.raises(TheOddsApiPayloadError, match="response.*sha256|body_sha256"):
        provider.read_batch()
