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
