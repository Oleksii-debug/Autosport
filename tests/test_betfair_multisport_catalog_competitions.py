import pytest

from autosport.betfair_multisport_catalog import (
    LIST_COMPETITIONS,
    BetfairCatalogError,
    BetfairCompetition,
    build_list_competitions_request,
    build_list_events_request,
    build_list_market_catalogue_request,
    build_list_market_types_request,
    parse_competitions_result,
    parse_market_catalogue_result,
)


def _catalogue_row(*, competition_id="31", include_competition=True):
    row = {
        "marketId": "1.234567890",
        "marketName": "Race Winner",
        "marketStartTime": "2026-09-22T15:00:00Z",
        "eventType": {"id": "8", "name": "Motor Sport"},
        "event": {"id": "9001", "name": "Grand Prix"},
        "description": {"marketType": "WIN"},
    }
    if include_competition:
        row["competition"] = {"id": competition_id, "name": "Formula 1"}
    return row


def test_list_competitions_uses_provider_native_event_type_scope():
    request = build_list_competitions_request(event_type_ids=("8",))
    assert request.method == LIST_COMPETITIONS
    assert request.rpc_params() == {"filter": {"eventTypeIds": ["8"]}}


def test_parse_competitions_preserves_provider_identity_separate_from_name():
    parsed = parse_competitions_result(
        [
            {
                "competition": {"id": "31", "name": "Formula 1"},
                "marketCount": 42,
            }
        ]
    )
    assert parsed == (BetfairCompetition("31", "Formula 1", 42),)
    assert parsed[0].competition_id == "31"
    assert parsed[0].display_name == "Formula 1"


def test_parse_competitions_rejects_duplicate_provider_identity():
    with pytest.raises(BetfairCatalogError, match="duplicate competition id"):
        parse_competitions_result(
            [
                {
                    "competition": {"id": "31", "name": "Formula 1"},
                    "marketCount": 42,
                },
                {
                    "competition": {"id": "31", "name": "Localized Name"},
                    "marketCount": 1,
                },
            ]
        )


@pytest.mark.parametrize(
    "builder",
    [build_list_events_request, build_list_market_types_request],
)
def test_downstream_discovery_can_bind_competition_ids(builder):
    request = builder(event_type_ids=("8",), competition_ids=("31",))
    assert request.rpc_params()["filter"] == {
        "eventTypeIds": ["8"],
        "competitionIds": ["31"],
    }


def test_catalogue_request_binds_competition_scope_and_requests_projection():
    request = build_list_market_catalogue_request(
        event_type_ids=("8",),
        competition_ids=("31",),
        market_type_codes=("WIN",),
        max_results=100,
    )
    params = request.rpc_params()
    assert params["filter"]["competitionIds"] == ["31"]
    assert "COMPETITION" in params["marketProjection"]


def test_catalogue_result_accepts_exact_requested_competition_identity():
    batch = parse_market_catalogue_result(
        [_catalogue_row()],
        requested_event_type_ids=("8",),
        requested_competition_ids=("31",),
        requested_market_type_codes=("WIN",),
        requested_max_results=100,
    )
    assert len(batch.markets) == 1
    assert batch.markets[0].competition_id == "31"


def test_catalogue_result_rejects_other_competition_under_scoped_request():
    with pytest.raises(BetfairCatalogError, match="competition scope"):
        parse_market_catalogue_result(
            [_catalogue_row(competition_id="999")],
            requested_event_type_ids=("8",),
            requested_competition_ids=("31",),
            requested_market_type_codes=("WIN",),
            requested_max_results=100,
        )


def test_catalogue_result_rejects_missing_competition_under_scoped_request():
    with pytest.raises(BetfairCatalogError, match="competition scope"):
        parse_market_catalogue_result(
            [_catalogue_row(include_competition=False)],
            requested_event_type_ids=("8",),
            requested_competition_ids=("31",),
            requested_market_type_codes=("WIN",),
            requested_max_results=100,
        )


def test_unscoped_catalogue_does_not_invent_competition_identity():
    batch = parse_market_catalogue_result(
        [_catalogue_row(include_competition=False)],
        requested_event_type_ids=("8",),
        requested_market_type_codes=("WIN",),
        requested_max_results=100,
    )
    assert batch.markets[0].competition_id is None


def test_duplicate_competition_filter_ids_fail_closed():
    with pytest.raises(BetfairCatalogError, match="competition_ids must not contain duplicates"):
        build_list_market_catalogue_request(
            event_type_ids=("8",),
            competition_ids=("31", "31"),
        )


def test_competition_identity_is_not_derived_from_localized_name():
    a = BetfairCompetition("31", "Formula 1", 42)
    b = BetfairCompetition("31", "Формула 1", 42)
    assert a.competition_id == b.competition_id
    assert a.display_name != b.display_name
