from __future__ import annotations

import json
import urllib.request as _urllib_request

import pytest

from autosport import _betfair_settlement_credential_origin_guard as origin_guard
from autosport.betfair_account_identity import build_betfair_authenticated_client
from autosport.betfair_account_readonly import BetfairReadOnlyClient, BetfairSessionCredentials
from autosport.betfair_settlement_revisions import (
    BetfairSettlementRevisionError,
    read_currency_qualified_execution_readback,
)


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


class _RoutingProvider:
    def __init__(self) -> None:
        self.client: BetfairReadOnlyClient | None = None
        self.mutate_account_on_method: str | None = None
        self.calls: list[str] = []

    def response(self, request) -> _Response:
        assert request.data is not None
        rpc = json.loads(request.data.decode("utf-8"))
        method = rpc["method"]
        self.calls.append(method)
        if self.mutate_account_on_method == method:
            assert self.client is not None
            self.client._account_id = "acct-poisoned"

        if method.endswith("getAccountDetails"):
            result = {
                "currencyCode": "USD",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "UTC",
            }
        elif method.endswith("listMarketCatalogue"):
            result = [{"marketId": "1.234", "event": {"id": "event-1"}}]
        elif method.endswith("listCurrentOrders"):
            result = {"currentOrders": [], "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            rows = []
            if rpc["params"]["betStatus"] == "SETTLED":
                rows.append(
                    {
                        "betId": "bet-777",
                        "marketId": "1.234",
                        "eventId": "event-1",
                        "selectionId": 10,
                        "side": "BACK",
                        "placedDate": "2026-09-28T21:00:00+00:00",
                        "settledDate": "2026-09-28T22:00:00+00:00",
                        "priceRequested": 2,
                        "priceMatched": 2,
                        "sizeSettled": 5,
                        "profit": 4,
                        "customerOrderRef": "provider-ref-1",
                        "betOutcome": "WON",
                    }
                )
            result = {"clearedOrders": rows, "moreAvailable": False}
        else:  # pragma: no cover
            raise AssertionError(method)

        payload = json.dumps(
            {"jsonrpc": "2.0", "id": rpc["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")
        return _Response(payload)


class _Opener:
    def __init__(self, provider: _RoutingProvider) -> None:
        self._provider = provider

    def open(self, request, data=None, timeout: float = 0):
        assert data is None
        assert timeout > 0
        return self._provider.response(request)


def _client(monkeypatch) -> tuple[BetfairReadOnlyClient, _RoutingProvider]:
    provider = _RoutingProvider()
    monkeypatch.setattr(_urllib_request, "_opener", _Opener(provider))
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    provider.client = client
    return client, provider


def _qualified_capture(client: BetfairReadOnlyClient):
    return read_currency_qualified_execution_readback(
        client,
        action_id="action-1",
        market_id="1.234",
        provider_order_ref="provider-ref-1",
    )


def test_account_route_change_during_execution_readback_revokes_currency_authority(
    monkeypatch,
) -> None:
    client, provider = _client(monkeypatch)
    provider.mutate_account_on_method = "SportsAPING/v1.0/listMarketCatalogue"

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="routing identity changed during readback",
    ):
        _qualified_capture(client)

    assert client._account_id == "acct-poisoned"
    assert any(method.endswith("listMarketCatalogue") for method in provider.calls)


def test_account_route_change_after_capture_revokes_currency_persistence(
    monkeypatch,
) -> None:
    client, _provider = _client(monkeypatch)
    capture = _qualified_capture(client)
    assert origin_guard._currency_for_capture(capture) == "USD"

    client._account_id = "acct-poisoned"

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="authenticated context changed before persistence",
    ):
        origin_guard._currency_for_capture(capture)


def test_k07_verifier_executable_drift_cannot_preserve_currency_after_credential_rotation(
    monkeypatch,
) -> None:
    client, _provider = _client(monkeypatch)
    capture = _qualified_capture(client)
    assert origin_guard._currency_for_capture(capture) == "USD"

    client._credentials = BetfairSessionCredentials(
        "app-key-rotated",
        "session-token-rotated",
    )

    def permissive_verifier(identity, *, client):
        return identity

    monkeypatch.setattr(
        origin_guard._REQUIRE_IDENTITY,
        "__code__",
        permissive_verifier.__code__,
    )

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="authenticated identity verifier authority changed",
    ):
        origin_guard._currency_for_capture(capture)


def test_k07_verifier_alias_rebind_is_rejected_before_rebound_execution(
    monkeypatch,
) -> None:
    client, _provider = _client(monkeypatch)
    capture = _qualified_capture(client)
    calls: list[object] = []

    def permissive_verifier(identity, *, client):
        calls.append(client)
        return identity

    monkeypatch.setattr(origin_guard, "_REQUIRE_IDENTITY", permissive_verifier)

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="authenticated identity verifier authority changed",
    ):
        origin_guard._currency_for_capture(capture)

    assert calls == []


def test_k07_resolver_alias_rebind_is_rejected_before_rebound_execution(
    monkeypatch,
) -> None:
    client, _provider = _client(monkeypatch)
    calls: list[object] = []

    def hostile_resolver(value):
        calls.append(value)
        raise AssertionError("rebound K07 identity resolver executed")

    monkeypatch.setattr(origin_guard, "_RESOLVE_IDENTITY", hostile_resolver)

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="authenticated identity verifier authority changed",
    ):
        _qualified_capture(client)

    assert calls == []


def test_k07_resolver_executable_drift_is_rejected_before_execution(
    monkeypatch,
) -> None:
    client, _provider = _client(monkeypatch)

    def hostile_resolver(value):
        raise AssertionError("mutated K07 identity resolver executed")

    monkeypatch.setattr(
        origin_guard._RESOLVE_IDENTITY,
        "__code__",
        hostile_resolver.__code__,
    )

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="authenticated identity verifier authority changed",
    ):
        _qualified_capture(client)


def test_qualified_read_alias_rebind_is_rejected_before_rebound_execution(
    monkeypatch,
) -> None:
    client, _provider = _client(monkeypatch)
    calls: list[object] = []

    def hostile_read(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("rebound settlement qualified-read authority executed")

    monkeypatch.setattr(origin_guard, "_ORIGINAL_QUALIFIED_READ", hostile_read)

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="qualified read authority changed",
    ):
        _qualified_capture(client)

    assert calls == []


def test_currency_lookup_alias_rebind_is_rejected_before_rebound_execution(
    monkeypatch,
) -> None:
    client, _provider = _client(monkeypatch)
    capture = _qualified_capture(client)
    calls: list[object] = []

    def hostile_lookup(value):
        calls.append(value)
        return "USD"

    monkeypatch.setattr(
        origin_guard,
        "_ORIGINAL_CURRENCY_FOR_CAPTURE",
        hostile_lookup,
    )

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="evidence lookup authority changed",
    ):
        origin_guard._currency_for_capture(capture)

    assert calls == []
