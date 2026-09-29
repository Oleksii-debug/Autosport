from __future__ import annotations

import json
import urllib.request as _urllib_request

import pytest

from autosport.betfair_account_identity import build_betfair_authenticated_client
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


_ACCOUNT_DETAILS_METHOD = "AccountAPING/v1.0/getAccountDetails"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        assert limit >= len(self._payload)
        return self._payload


class _QueuedOpener:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)

    def open(self, request, data=None, timeout: float = 0):
        assert data is None
        assert request.data is not None
        assert timeout > 0
        rpc_request = json.loads(request.data.decode("utf-8"))
        method = rpc_request["method"]
        if method == _ACCOUNT_DETAILS_METHOD:
            assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
            payload = {
                "jsonrpc": "2.0",
                "id": rpc_request["id"],
                "result": {
                    "currencyCode": "EUR",
                    "localeCode": "en",
                    "region": "GBR",
                    "timezone": "Europe/London",
                },
            }
        else:
            assert request.full_url == BETTING_JSON_RPC_ENDPOINT
            if not self._responses:
                raise AssertionError(f"unexpected Betfair readback call: {method}")
            payload = json.loads(self._responses.pop(0).decode("utf-8"))
            # Existing focused fixtures encode deterministic request ids for a
            # direct client. K07 consumes one account-details id first, so bind
            # the same provider result to the actual canonical request id.
            payload["id"] = rpc_request["id"]
        return _Response(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        )

    def assert_drained(self) -> None:
        assert not self._responses, "unused Betfair readback fixture responses"


def authoritative_execution_readback(
    responses: list[bytes],
    *,
    action_id: str,
    market_id: str,
    provider_order_ref: str | None,
    account_id: str,
    page_size: int = 1000,
    max_pages: int = 100,
):
    """Capture through the real K07 product-client origin with deterministic I/O."""

    opener = _QueuedOpener(responses)
    with pytest.MonkeyPatch.context() as monkeypatch:
        # Preserve autosport.betfair_account_readonly.urlopen and its canonical
        # transport identity. Only stdlib's process opener below it is replaced.
        monkeypatch.setattr(_urllib_request, "_opener", opener)
        client = build_betfair_authenticated_client(
            BetfairSessionCredentials("fixture-app-key", "fixture-session-token"),
            account_label=account_id,
        )
        capture = client.read_execution_readback(
            action_id=action_id,
            market_id=market_id,
            provider_order_ref=provider_order_ref,
            page_size=page_size,
            max_pages=max_pages,
        )
    opener.assert_drained()
    return capture


def authoritative_execution_readback_with_client(
    responses: list[bytes],
    *,
    action_id: str,
    market_id: str,
    provider_order_ref: str | None,
    account_id: str,
    page_size: int = 1000,
    max_pages: int = 100,
) -> tuple[BetfairReadOnlyClient, object]:
    opener = _QueuedOpener(responses)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(_urllib_request, "_opener", opener)
        client = build_betfair_authenticated_client(
            BetfairSessionCredentials("fixture-app-key", "fixture-session-token"),
            account_label=account_id,
        )
        capture = client.read_execution_readback(
            action_id=action_id,
            market_id=market_id,
            provider_order_ref=provider_order_ref,
            page_size=page_size,
            max_pages=max_pages,
        )
    opener.assert_drained()
    return client, capture
