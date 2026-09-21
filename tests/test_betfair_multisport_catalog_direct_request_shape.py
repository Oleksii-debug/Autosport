from __future__ import annotations

import pytest

from autosport.betfair_multisport_catalog import (
    LIST_EVENTS,
    LIST_EVENT_TYPES,
    LIST_MARKET_CATALOGUE,
    LIST_MARKET_TYPES,
    BetfairCatalogError,
    BetfairCatalogRequest,
)


@pytest.mark.parametrize(
    "method",
    [LIST_EVENT_TYPES, LIST_EVENTS, LIST_MARKET_TYPES],
)
def test_direct_discovery_request_requires_market_filter(method: str) -> None:
    with pytest.raises(BetfairCatalogError, match="filter"):
        BetfairCatalogRequest(method, {})


def test_direct_catalogue_request_requires_market_filter() -> None:
    with pytest.raises(BetfairCatalogError, match="filter"):
        BetfairCatalogRequest(
            LIST_MARKET_CATALOGUE,
            {"maxResults": 1},
        )


@pytest.mark.parametrize(
    "max_results",
    [None, True, 0, 1001],
)
def test_direct_catalogue_request_requires_valid_provider_limit(
    max_results: object,
) -> None:
    params: dict[str, object] = {"filter": {}}
    if max_results is not None:
        params["maxResults"] = max_results

    with pytest.raises(BetfairCatalogError, match=r"maxResults|1\.\.1000"):
        BetfairCatalogRequest(LIST_MARKET_CATALOGUE, params)
