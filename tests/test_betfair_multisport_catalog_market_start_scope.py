from __future__ import annotations

import pytest

from autosport.betfair_multisport_catalog import (
    BetfairCatalogError,
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


def _parse(row: dict[str, object], *, start: str, end: str):
    return parse_market_catalogue_result(
        [row],
        requested_event_type_ids=("1",),
        requested_event_ids=("event-1",),
        requested_market_type_codes=("MATCH_ODDS",),
        requested_max_results=10,
        requested_market_start_from=start,
        requested_market_start_to=end,
    )


@pytest.mark.parametrize(
    "outside",
    ["2026-09-21T09:59:59.999Z", "2026-09-21T11:00:00.001Z"],
)
def test_catalogue_row_outside_requested_market_start_window_fails_closed(
    outside: str,
) -> None:
    with pytest.raises(BetfairCatalogError, match="time|start|scope|window"):
        _parse(
            _catalogue_row(market_start_time=outside),
            start="2026-09-21T10:00:00.000Z",
            end="2026-09-21T11:00:00.000Z",
        )


@pytest.mark.parametrize(
    "inside",
    [
        "2026-09-21T10:00:00.000Z",
        "2026-09-21T10:30:00+00:00",
        "2026-09-21T11:00:00.000Z",
    ],
)
def test_catalogue_row_inside_or_on_requested_window_is_accepted(inside: str) -> None:
    batch = _parse(
        _catalogue_row(market_start_time=inside),
        start="2026-09-21T10:00:00.000Z",
        end="2026-09-21T11:00:00.000Z",
    )
    assert batch.markets[0].market_start_time == inside


def test_catalogue_result_rejects_unparseable_provider_market_start_time() -> None:
    with pytest.raises(BetfairCatalogError, match="market_start_time"):
        _parse(
            _catalogue_row(market_start_time="not-a-date"),
            start="2026-09-21T10:00:00.000Z",
            end="2026-09-21T11:00:00.000Z",
        )
