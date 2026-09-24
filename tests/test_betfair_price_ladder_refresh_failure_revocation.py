from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_price_ladder_admission import (
    BetfairPriceLadderAuthority,
    BetfairPriceLadderError,
    PriceLadderAdmissionState,
)


FIXED_NOW = datetime(2026, 9, 22, 12, 1, tzinfo=timezone.utc)


class SequencedTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        if not self._responses:
            raise AssertionError("unexpected transport call")
        return self._responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _market_row(
    market_id: str,
    ladder_type: str = "CLASSIC",
) -> dict[str, object]:
    return {
        "marketId": market_id,
        "description": {
            "priceLadderDescription": {"type": ladder_type},
        },
    }


def _authority_for(*responses: bytes) -> BetfairPriceLadderAuthority:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=SequencedTransport(list(responses)),
        clock=lambda: FIXED_NOW,
    )
    return BetfairPriceLadderAuthority(client)


def test_failed_same_market_refresh_revokes_prior_positive_admission() -> None:
    authority = _authority_for(
        _response([_market_row("1.234")], 1),
        _response([], 2),
    )

    observation = authority.acquire("1.234")
    admission = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
    )
    assert (
        admission.state
        is PriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    )
    admission.assert_authoritative()

    with pytest.raises(
        BetfairPriceLadderError,
        match="exact market definition",
    ):
        authority.acquire("1.234")

    # A failed current refresh establishes that the authority no longer has a
    # current trusted definition for this exact market. The previous positive
    # witness may remain auditable, but it must not stay consumable as current
    # provider order-admission authority.
    with pytest.raises(BetfairPriceLadderError):
        admission.assert_authoritative()

    with pytest.raises(BetfairPriceLadderError):
        authority.resolve(
            observation=observation,
            market_id="1.234",
            price="2.00",
        )


def test_failed_other_market_refresh_does_not_revoke_unrelated_market() -> None:
    authority = _authority_for(
        _response([_market_row("1.234")], 1),
        _response([], 2),
    )

    observation = authority.acquire("1.234")
    admission = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
    )

    with pytest.raises(
        BetfairPriceLadderError,
        match="exact market definition",
    ):
        authority.acquire("1.999")

    # Refresh failure is market-scoped. A failed lookup for another market
    # must not act as a global kill switch for independently acquired evidence.
    observation.assert_authoritative()
    admission.assert_authoritative()
