from __future__ import annotations

import pytest

from autosport.betfair_multisport_catalog import (
    BetfairCatalogError,
    build_list_market_catalogue_request,
)


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("market_start_from", {"market_start_from": "not-a-date"}),
        ("market_start_to", {"market_start_to": "not-a-date"}),
    ],
)
def test_catalogue_request_rejects_non_date_market_start_bounds(
    field: str,
    kwargs: dict[str, str],
) -> None:
    with pytest.raises(BetfairCatalogError, match=field):
        build_list_market_catalogue_request(
            event_type_ids=("1",),
            **kwargs,
        )


def test_catalogue_request_rejects_naive_market_start_bound() -> None:
    with pytest.raises(BetfairCatalogError, match="market_start_from"):
        build_list_market_catalogue_request(
            event_type_ids=("1",),
            market_start_from="2026-09-21T10:00:00",
        )


def test_catalogue_request_rejects_reversed_market_start_window() -> None:
    with pytest.raises(BetfairCatalogError, match="must not be after"):
        build_list_market_catalogue_request(
            event_type_ids=("1",),
            market_start_from="2026-09-21T11:00:00Z",
            market_start_to="2026-09-21T10:00:00Z",
        )
