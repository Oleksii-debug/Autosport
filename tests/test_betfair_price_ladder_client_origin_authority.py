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


class CallerControlledTransport:
    """A locally injected transport that never contacts Betfair."""

    def __init__(self, ladder_type: str) -> None:
        self._ladder_type = ladder_type

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": [
                    {
                        "marketId": "1.234",
                        "description": {
                            "priceLadderDescription": {
                                "type": self._ladder_type,
                            }
                        },
                    }
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8")


def _assert_caller_transport_cannot_mint_positive_authority(
    ladder_type: str,
    price: str,
) -> None:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("caller-app-key", "caller-session-token"),
        transport=CallerControlledTransport(ladder_type),
        clock=lambda: datetime(2026, 9, 22, 9, 48, tzinfo=timezone.utc),
    )
    authority = BetfairPriceLadderAuthority(client)

    try:
        observation = authority.acquire("1.234")
        admission = authority.resolve(
            observation=observation,
            market_id="1.234",
            price=price,
        )
        admission.assert_authoritative()
    except BetfairPriceLadderError:
        # A product-owned origin fence may reject the injected client/transport
        # at construction, acquisition, resolution, or authority verification.
        return

    assert (
        admission.state is not PriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    ), (
        "caller-controlled local transport minted authoritative Betfair "
        "PRICE_LADDER_ADMISSIBLE evidence"
    )


@pytest.mark.parametrize(
    ("ladder_type", "price"),
    (
        ("CLASSIC", "2.00"),
        ("FINEST", "2.01"),
    ),
)
def test_caller_controlled_exact_client_cannot_mint_positive_price_ladder_authority(
    ladder_type: str,
    price: str,
) -> None:
    _assert_caller_transport_cannot_mint_positive_authority(
        ladder_type,
        price,
    )
