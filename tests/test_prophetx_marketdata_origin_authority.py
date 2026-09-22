from __future__ import annotations

from autosport.prophetx_marketdata import (
    ProphetXJsonResponse,
    ProphetXRestMarketProvider,
)


def _caller_authored_response(payload: object) -> ProphetXJsonResponse:
    return ProphetXJsonResponse(
        payload=payload,
        status_code=200,
        headers={},
        body_sha256="0" * 64,
    )


def test_injected_transport_cannot_mint_provider_declared_empty_origin_truth() -> None:
    """A caller-authored transport result must not look like fixed-origin provider truth."""

    provider = ProphetXRestMarketProvider(
        "caller-known-test-token",
        (1001,),
        transport=lambda *_: _caller_authored_response({"data": {"markets": []}}),
        clock=lambda: "2026-09-22T19:00:00+00:00",
    )

    batch = provider.read_batch()

    assert "UNVERIFIED_PROVIDER_ORIGIN" in batch.quality_flags
    assert "PROVIDER_DECLARED_EMPTY_MARKET" not in batch.quality_flags


def test_injected_transport_quotes_are_machine_marked_origin_unverified() -> None:
    """Synthetic test transport evidence cannot inherit ProphetX network provenance."""

    payload = {
        "data": {
            "markets": [
                {
                    "event_id": 1001,
                    "market_id": "market-1",
                    "type": "moneyline",
                    "status": "open",
                    "selections": [
                        [
                            {
                                "strike_id": "strike-a",
                                "outcome_id": "outcome-a",
                                "price": 150,
                                "quantity": "1",
                            }
                        ]
                    ],
                }
            ]
        }
    }
    provider = ProphetXRestMarketProvider(
        "caller-known-test-token",
        (1001,),
        transport=lambda *_: _caller_authored_response(payload),
        clock=lambda: "2026-09-22T19:00:00+00:00",
    )

    batch = provider.read_batch()

    assert "UNVERIFIED_PROVIDER_ORIGIN" in batch.quality_flags
    assert len(batch.quotes) == 1
    assert batch.quotes[0].metadata["provider_origin_verified"] is False
