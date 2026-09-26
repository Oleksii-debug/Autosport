from collections.abc import Mapping

from autosport.betfair_multisport_catalog import (
    LIST_EVENTS,
    BetfairCatalogRequest,
    build_list_market_catalogue_request,
)


def test_request_does_not_retain_mutable_nested_caller_aliases() -> None:
    caller_filter = {"eventTypeIds": ["1"]}
    request = BetfairCatalogRequest(LIST_EVENTS, {"filter": caller_filter})

    caller_filter["eventTypeIds"].append("7")

    filter_view = request.params["filter"]
    assert isinstance(filter_view, Mapping)
    assert tuple(filter_view["eventTypeIds"]) == ("1",)


def test_issued_request_filter_ids_cannot_be_mutated_after_validation() -> None:
    request = build_list_market_catalogue_request(
        event_type_ids=("1",),
        market_type_codes=("MATCH_ODDS",),
    )
    filter_view = request.params["filter"]
    assert isinstance(filter_view, Mapping)
    event_type_ids = filter_view["eventTypeIds"]

    try:
        event_type_ids.append("7")  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass

    assert tuple(filter_view["eventTypeIds"]) == ("1",)


def test_issued_request_projection_and_time_range_cannot_be_mutated() -> None:
    original_from = "2026-09-21T00:00:00Z"
    request = build_list_market_catalogue_request(
        event_type_ids=("1",),
        market_start_from=original_from,
        market_start_to="2026-09-22T00:00:00Z",
    )
    projection = request.params["marketProjection"]
    filter_view = request.params["filter"]
    assert isinstance(filter_view, Mapping)
    time_range = filter_view["marketStartTime"]
    assert isinstance(time_range, Mapping)

    try:
        projection.append("RUNNER_DESCRIPTION")  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass
    try:
        time_range["from"] = "2099-01-01T00:00:00Z"  # type: ignore[index]
    except (AttributeError, TypeError):
        pass

    assert "RUNNER_DESCRIPTION" not in tuple(request.params["marketProjection"])
    assert time_range["from"] == original_from
