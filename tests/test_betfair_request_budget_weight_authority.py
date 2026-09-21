from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autosport.betfair_request_budget import (
    LIST_MARKET_BOOK,
    BetfairRequestBudget,
    BetfairRequestBudgetError,
    BudgetedBetfairReadTransport,
)


ENDPOINT = "https://api.betfair.com/exchange/betting/json-rpc/v1"


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(body)
        return b'{"jsonrpc":"2.0","result":[],"id":1}'


def _body() -> bytes:
    # Betfair's current Market Data Request Limits table assigns EX_ALL_OFFERS
    # weight 17. Twelve markets therefore cost 12 * 17 = 204 points, above the
    # documented 200-point per-request ceiling.
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": LIST_MARKET_BOOK,
            "params": {
                "marketIds": [f"1.{index:03d}" for index in range(12)],
                "priceProjection": {"priceData": ["EX_ALL_OFFERS"]},
            },
            "id": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_caller_lowball_weight_resolver_cannot_bypass_200_point_ceiling(
    tmp_path: Path,
) -> None:
    transport = _FakeTransport()
    budget = BetfairRequestBudget(
        tmp_path / "request-budget.json",
        clock=lambda: datetime(2026, 9, 21, 18, 10, tzinfo=timezone.utc),
    )

    # The admission contract must not let an arbitrary integration callback turn a
    # provider-defined 204-point request into a locally asserted one-point request.
    wrapped = BudgetedBetfairReadTransport(
        transport,
        budget,
        market_data_weight_resolver=lambda params: 1,
    )

    with pytest.raises(BetfairRequestBudgetError):
        wrapped.post(
            ENDPOINT,
            headers={},
            body=_body(),
            timeout_seconds=1,
        )

    assert transport.calls == []
