from __future__ import annotations

import pytest

from autosport.betfair_multisport_catalog import (
    BetfairCatalogError,
    BetfairCatalogRequest,
    LIST_MARKET_CATALOGUE,
    build_list_event_types_request,
    build_list_market_catalogue_request,
    parse_market_catalogue_result,
    parse_market_catalogue_result_for_request,
)


def _row(
    *,
    event_id: str = "event-1",
    market_start_time: str = "2026-09-22T12:00:00Z",
) -> dict[str, object]:
    return {
        "marketId": "1.234",
        "marketName": "Match Odds",
        "marketStartTime": market_start_time,
        "eventType": {"id": "1", "name": "Soccer"},
        "competition": {"id": "competition-1", "name": "League"},
        "event": {"id": event_id, "name": "A v B"},
        "description": {"marketType": "MATCH_ODDS"},
    }


def _request():
    return build_list_market_catalogue_request(
        event_type_ids=("1",),
        competition_ids=("competition-1",),
        event_ids=("event-1",),
        market_type_codes=("MATCH_ODDS",),
        max_results=2,
        market_start_from="2026-09-22T00:00:00Z",
        market_start_to="2026-09-23T00:00:00Z",
    )


def test_exact_request_scope_is_the_positive_parse_authority() -> None:
    request = _request()

    batch = parse_market_catalogue_result_for_request([_row()], request=request)

    assert batch.requested_max_results == 2
    assert batch.response_not_limit_saturated is True
    assert batch.markets[0].event_id == "event-1"
    assert batch.markets[0].competition_id == "competition-1"
    assert batch.markets[0].market_type_code == "MATCH_ODDS"


def test_caller_cannot_replace_issued_request_scope_during_positive_parse() -> None:
    request = _request()
    escaped = _row(event_id="event-2")

    # The compatibility parser can only validate the assertions it is handed.
    # A caller could therefore describe the same row as if event-2 had been the
    # requested scope. Downstream authority must use the request-bound seam.
    legacy = parse_market_catalogue_result(
        [escaped],
        requested_event_type_ids=("1",),
        requested_competition_ids=("competition-1",),
        requested_event_ids=("event-2",),
        requested_market_type_codes=("MATCH_ODDS",),
        requested_max_results=2,
        requested_market_start_from="2026-09-22T00:00:00Z",
        requested_market_start_to="2026-09-23T00:00:00Z",
    )
    assert legacy.markets[0].event_id == "event-2"

    with pytest.raises(BetfairCatalogError, match="requested event scope"):
        parse_market_catalogue_result_for_request([escaped], request=request)


def test_request_bound_parse_rejects_non_catalogue_request() -> None:
    with pytest.raises(BetfairCatalogError, match="listMarketCatalogue"):
        parse_market_catalogue_result_for_request(
            [_row()],
            request=build_list_event_types_request(),
        )


def test_request_bound_parse_rejects_unmodelled_time_range_fields() -> None:
    request = BetfairCatalogRequest(
        LIST_MARKET_CATALOGUE,
        {
            "filter": {
                "eventTypeIds": ["1"],
                "marketStartTime": {
                    "from": "2026-09-22T00:00:00Z",
                    "unexpected": "caller-only-scope",
                },
            },
            "maxResults": 2,
        },
    )

    with pytest.raises(BetfairCatalogError, match="marketStartTime.*unsupported"):
        parse_market_catalogue_result_for_request([_row()], request=request)
