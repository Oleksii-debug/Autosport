import json
import pytest

from autosport.betfair_multisport_catalog import (
    BetfairCatalogError,
    build_list_event_types_request,
    build_list_events_request,
    build_list_market_catalogue_request,
    build_list_market_types_request,
    parse_event_types_result,
    parse_events_result,
    parse_market_catalogue_result,
    parse_market_types_result,
)


def test_event_type_discovery_uses_provider_native_read_method():
    request = build_list_event_types_request()
    assert request.method == "SportsAPING/v1.0/listEventTypes"
    assert request.params == {"filter": {}}


def test_events_and_market_types_are_scoped_by_event_type_id_not_name():
    events = build_list_events_request(event_type_ids=("1", "7"))
    types = build_list_market_types_request(event_type_ids=("1", "7"))
    assert events.params["filter"] == {"eventTypeIds": ("1", "7")}
    assert types.params["filter"] == {"eventTypeIds": ("1", "7")}


def test_catalogue_request_uses_locale_independent_selectors_and_provider_limit():
    request = build_list_market_catalogue_request(
        event_type_ids=("1", "7"),
        event_ids=("100", "200"),
        market_type_codes=("MATCH_ODDS", "WIN"),
        max_results=1000,
        market_start_from="2026-09-21T00:00:00Z",
        market_start_to="2026-09-22T00:00:00Z",
    )
    assert request.method == "SportsAPING/v1.0/listMarketCatalogue"
    assert request.params["filter"] == {
        "eventTypeIds": ("1", "7"),
        "eventIds": ("100", "200"),
        "marketTypeCodes": ("MATCH_ODDS", "WIN"),
        "marketStartTime": {
            "from": "2026-09-21T00:00:00Z",
            "to": "2026-09-22T00:00:00Z",
        },
    }
    assert request.params["maxResults"] == 1000
    assert "EVENT_TYPE" in request.params["marketProjection"]
    assert "MARKET_DESCRIPTION" in request.params["marketProjection"]


@pytest.mark.parametrize("limit", [0, 1001, True, 1.5, "1000"])
def test_catalogue_request_rejects_invalid_provider_limit(limit):
    with pytest.raises(BetfairCatalogError, match="1..1000"):
        build_list_market_catalogue_request(event_type_ids=("1",), max_results=limit)


def test_provider_codes_are_not_replaced_by_localized_names():
    parsed = parse_event_types_result(
        [
            {"eventType": {"id": "1", "name": "Fußball"}, "marketCount": 12},
            {"eventType": {"id": "7", "name": "Pferderennen"}, "marketCount": 9},
        ]
    )
    assert [item.event_type_id for item in parsed] == ["1", "7"]
    assert [item.display_name for item in parsed] == ["Fußball", "Pferderennen"]


def test_duplicate_event_type_identity_fails_closed():
    with pytest.raises(BetfairCatalogError, match="duplicate eventType"):
        parse_event_types_result(
            [
                {"eventType": {"id": "1", "name": "Soccer"}, "marketCount": 2},
                {"eventType": {"id": "1", "name": "Football"}, "marketCount": 2},
            ]
        )


def test_events_and_market_types_parse_provider_native_identity():
    events = parse_events_result(
        [
            {
                "event": {
                    "id": "345",
                    "name": "A v B",
                    "countryCode": "GB",
                    "timezone": "Europe/London",
                    "openDate": "2026-09-21T18:00:00.000Z",
                },
                "marketCount": 4,
            }
        ]
    )
    market_types = parse_market_types_result(
        [{"marketType": "MATCH_ODDS", "marketCount": 4}]
    )
    assert events[0].event_id == "345"
    assert events[0].market_count == 4
    assert market_types[0].market_type_code == "MATCH_ODDS"


def _catalogue_row(index: int, *, event_type_id: str = "1") -> dict[str, object]:
    return {
        "marketId": f"1.{index}",
        "marketName": "Match Odds",
        "marketStartTime": "2026-09-21T18:00:00.000Z",
        "eventType": {"id": event_type_id, "name": "Soccer"},
        "event": {"id": f"event-{index}", "name": "A v B"},
        "description": {"marketType": "MATCH_ODDS"},
    }


def test_catalogue_batch_below_limit_proves_only_not_limit_saturated():
    batch = parse_market_catalogue_result(
        [_catalogue_row(1), _catalogue_row(2)],
        requested_event_type_ids=("1",),
        requested_max_results=3,
    )
    assert batch.response_not_limit_saturated is True
    assert batch.completeness_proven is False
    assert batch.continuation_required is False
    assert [item.event_type_id for item in batch.markets] == ["1", "1"]
    assert [item.market_type_code for item in batch.markets] == ["MATCH_ODDS", "MATCH_ODDS"]


def test_full_catalogue_batch_never_claims_complete_universe():
    batch = parse_market_catalogue_result(
        [_catalogue_row(1), _catalogue_row(2)],
        requested_event_type_ids=("1",),
        requested_max_results=2,
    )
    assert batch.completeness_proven is False
    assert batch.continuation_required is True


def test_catalogue_row_outside_requested_sport_scope_fails_closed():
    with pytest.raises(BetfairCatalogError, match="escaped"):
        parse_market_catalogue_result(
            [_catalogue_row(1, event_type_id="7")],
            requested_event_type_ids=("1",),
            requested_max_results=10,
        )


def test_catalogue_duplicate_market_id_fails_closed():
    row = _catalogue_row(1)
    with pytest.raises(BetfairCatalogError, match="duplicate marketId"):
        parse_market_catalogue_result(
            [row, dict(row)],
            requested_event_type_ids=("1",),
            requested_max_results=10,
        )


def test_identity_lists_reject_duplicates_and_empty_scope():
    with pytest.raises(BetfairCatalogError, match="duplicates"):
        build_list_events_request(event_type_ids=("1", "1"))
    with pytest.raises(BetfairCatalogError, match="must not be empty"):
        build_list_market_types_request(event_type_ids=())


def test_catalogue_row_outside_requested_market_type_scope_fails_closed():
    with pytest.raises(BetfairCatalogError, match="marketType scope"):
        parse_market_catalogue_result(
            [_catalogue_row(1)],
            requested_event_type_ids=("1",),
            requested_market_type_codes=("WIN",),
            requested_max_results=10,
        )


def test_rpc_params_is_detached_and_json_serializable():
    request = build_list_market_catalogue_request(
        event_type_ids=("1",), market_type_codes=("MATCH_ODDS",)
    )
    params = request.rpc_params()
    json.dumps(params)
    params["filter"]["eventTypeIds"].append("7")
    assert tuple(request.params["filter"]["eventTypeIds"]) == ("1",)


def test_catalogue_row_outside_requested_event_scope_fails_closed():
    with pytest.raises(BetfairCatalogError, match="requested event scope"):
        parse_market_catalogue_result(
            [_catalogue_row(1)],
            requested_event_type_ids=("1",),
            requested_event_ids=("event-2",),
            requested_max_results=10,
        )
