from __future__ import annotations

import pytest

from autosport.betfair_multisport_catalog import (
    BetfairCatalogError,
    build_list_event_types_request,
)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_catalog_request_rejects_nonfinite_json_numbers(value: float) -> None:
    with pytest.raises(BetfairCatalogError):
        build_list_event_types_request(
            market_filter={"marketStartTime": {"from": value}}
        )
