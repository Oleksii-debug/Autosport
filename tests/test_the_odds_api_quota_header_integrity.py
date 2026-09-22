from __future__ import annotations

import pytest

from autosport.the_odds_api_reference import (
    TheOddsApiPayloadError,
    _quota_metadata,
)


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Requests-Remaining", "-1"),
        ("X-Requests-Used", "NaN"),
        ("X-Requests-Last", "garbage"),
    ],
)
def test_malformed_quota_provenance_fails_closed(header: str, value: str) -> None:
    with pytest.raises(TheOddsApiPayloadError, match="invalid quota header"):
        _quota_metadata({header: value})


def test_zero_last_request_cost_remains_valid_provider_evidence() -> None:
    assert _quota_metadata(
        {
            "X-Requests-Remaining": "500",
            "X-Requests-Used": "0",
            "X-Requests-Last": "0",
        }
    ) == {
        "x-requests-remaining": "500",
        "x-requests-used": "0",
        "x-requests-last": "0",
    }
