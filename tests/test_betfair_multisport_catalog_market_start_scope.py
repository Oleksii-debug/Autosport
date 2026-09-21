from __future__ import annotations

import inspect

import pytest

from autosport.betfair_multisport_catalog import (
    BetfairCatalogError,
    build_list_market_catalogue_request,
    parse_market_catalogue_result,
)


def _catalogue_row(*, market_start_time: str) -> dict[str, object]:
    return {
        "marketId": "1.234",
        "marketName": "Match Odds",
        "marketStartTime": market_start_time,
        "eventType": {"id": "1", "name": "Soccer"},
        "event": {"id": "event-1", "name": "A v B"},
        "description": {"marketType": "MATCH_ODDS"},
    }


def test_catalogue_row_outside_requested_market_start_window_fails_closed() -> None:
    market_start_from = "2026-09-21T10:00:00.000Z"
    market_start_to = "2026-09-21T11:00:00.000Z"
    request = build_list_market_catalogue_request(
        event_type_ids=("1",),
        event_ids=("event-1",),
        market_type_codes=("MATCH_ODDS",),
        max_results=10,
        market_start_from=market_start_from,
        market_start_to=market_start_to,
    )
    outside = _catalogue_row(market_start_time="2026-09-21T12:00:00.000Z")

    parameters = inspect.signature(parse_market_catalogue_result).parameters
    common = {
        "requested_event_type_ids": ("1",),
        "requested_event_ids": ("event-1",),
        "requested_market_type_codes": ("MATCH_ODDS",),
        "requested_max_results": 10,
    }

    if "request" in parameters:
        # Safest repair shape: bind response validation to the exact issued request.
        kwargs = {"request": request}
    elif {
        "requested_market_start_from",
        "requested_market_start_to",
    }.issubset(parameters):
        # Also acceptable: bind the exact requested time bounds explicitly.
        kwargs = {
            **common,
            "requested_market_start_from": market_start_from,
            "requested_market_start_to": market_start_to,
        }
    else:
        pytest.fail(
            "parse_market_catalogue_result cannot bind response rows to the "
            "requested marketStartTime window"
        )

    with pytest.raises(BetfairCatalogError, match="time|start|scope|window"):
        parse_market_catalogue_result([outside], **kwargs)
