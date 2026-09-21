from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_provider_billing_inputs import (
    BetfairProviderBillingInputsObservation,
    read_betfair_provider_billing_inputs,
)


class _Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return datetime(
            2026, 9, 21, 1, 50, self._tick, tzinfo=timezone.utc
        )


class _Transport:
    def __init__(self, application_key: str) -> None:
        self.application_key = application_key
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "request": request,
                "timeout_seconds": timeout_seconds,
            }
        )
        method = request["method"]
        if method == "AccountAPING/v1.0/getAccountDetails":
            result: object = {"currencyCode": "GBP"}
        elif method == "AccountAPING/v1.0/getDeveloperAppKeys":
            result = [
                {
                    "appId": 41,
                    "appName": "autosport",
                    "appVersions": [
                        {
                            "owner": "owner",
                            "versionId": 7,
                            "version": "1.0",
                            "applicationKey": self.application_key,
                            "delayData": False,
                            "subscriptionRequired": False,
                            "ownerManaged": False,
                            "active": True,
                            "vendorId": "vendor-3",
                        },
                        {
                            "owner": "owner",
                            "versionId": 8,
                            "version": "1.1-delayed",
                            "applicationKey": "other-key",
                            "delayData": True,
                            "subscriptionRequired": False,
                            "ownerManaged": False,
                            "active": True,
                            "vendorId": "vendor-3",
                        },
                    ],
                }
            ]
        elif method == "AccountAPING/v1.0/getAccountStatement":
            result = {
                "accountStatement": [
                    {
                        "refId": "billing-ref-1",
                        "itemDate": "2026-09-20T09:00:00Z",
                        "amount": "-499.00",
                        "balance": "1501.00",
                        "itemClass": "ACCOUNT_DEBIT",
                        "itemClassData": {"source": "provider"},
                    }
                ],
                "moreAvailable": False,
            }
        else:  # pragma: no cover - the capability has a closed method set
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _client(
    *, application_key: str = "live-key-123"
) -> tuple[BetfairReadOnlyClient, _Transport]:
    transport = _Transport(application_key)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials(application_key, "session-secret"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="account-A",
    )
    return client, transport


def test_captures_exact_entitlement_and_statement_without_minting_cost() -> None:
    client, transport = _client()

    observation = read_betfair_provider_billing_inputs(
        client,
        statement_from="2026-09-01T00:00:00Z",
        statement_to="2026-09-21T00:00:00Z",
    )

    assert type(observation) is BetfairProviderBillingInputsObservation
    assert observation.entitlement.account_id == "account-A"
    assert observation.entitlement.active is True
    assert observation.entitlement.delay_data is False
    assert observation.entitlement.vendor_id == "vendor-3"
    assert observation.entitlement.application_key_sha256 == hashlib.sha256(
        b"live-key-123"
    ).hexdigest()
    assert observation.statement.currency_code == "GBP"
    assert observation.statement.items[0].amount == Decimal("-499.00")
    assert observation.statement.items[0].item_class == "ACCOUNT_DEBIT"
    assert not hasattr(observation, "cost_class")
    assert not hasattr(observation, "allocated_amount")

    assert [call["request"]["method"] for call in transport.calls] == [
        "AccountAPING/v1.0/getAccountDetails",
        "AccountAPING/v1.0/getDeveloperAppKeys",
        "AccountAPING/v1.0/getAccountStatement",
    ]
    assert all(
        call["url"] == ACCOUNT_JSON_RPC_ENDPOINT for call in transport.calls
    )
    assert transport.calls[2]["request"]["params"] == {
        "fromRecord": 0,
        "recordCount": 100,
        "itemDateRange": {
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-09-21T00:00:00Z",
        },
    }


def test_missing_statement_rows_are_preserved_as_absence_not_zero() -> None:
    client, transport = _client()
    original_post = transport.post

    def post(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        if request["method"] == "AccountAPING/v1.0/getAccountStatement":
            transport.calls.append(
                {
                    "url": url,
                    "headers": headers,
                    "request": request,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "accountStatement": [],
                        "moreAvailable": False,
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
        return original_post(
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    transport.post = post  # type: ignore[method-assign]
    observation = read_betfair_provider_billing_inputs(client)

    assert observation.statement.items == ()
    assert not hasattr(observation.statement, "known_zero")


def test_rejects_wrong_or_ambiguous_authenticated_application_key() -> None:
    client, transport = _client(application_key="missing-key")
    transport.application_key = "different-key"

    with pytest.raises(
        BetfairReadOnlyError,
        match="application key entitlement is ambiguous or missing",
    ):
        read_betfair_provider_billing_inputs(client)


def test_rejects_provider_rpc_error_without_exposing_provider_message() -> None:
    client, transport = _client()
    original_post = transport.post

    def post(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        if request["method"] == "AccountAPING/v1.0/getDeveloperAppKeys":
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {
                        "code": -32099,
                        "message": "secret provider detail",
                    },
                }
            ).encode("utf-8")
        return original_post(
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    transport.post = post  # type: ignore[method-assign]
    with pytest.raises(BetfairReadOnlyError) as exc_info:
        read_betfair_provider_billing_inputs(client)
    assert "secret provider detail" not in str(exc_info.value)


def test_rejects_polymorphic_client_and_instance_shadowed_authority() -> None:
    client, _ = _client()

    class _Subclass(BetfairReadOnlyClient):
        pass

    subclass = _Subclass(
        BetfairSessionCredentials("key", "token"),
        transport=_Transport("key"),
        clock=_Clock(),
        venue_id="betfair",
        account_id="account-A",
    )
    with pytest.raises(TypeError, match="exact BetfairReadOnlyClient"):
        read_betfair_provider_billing_inputs(subclass)

    client._rpc = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    with pytest.raises(BetfairReadOnlyError, match="instance-shadowed"):
        read_betfair_provider_billing_inputs(client)


def test_rejects_invalid_statement_window_before_provider_io() -> None:
    client, transport = _client()

    with pytest.raises(BetfairReadOnlyError, match="must not be after"):
        read_betfair_provider_billing_inputs(
            client,
            statement_from="2026-09-21T00:00:00Z",
            statement_to="2026-09-20T00:00:00Z",
        )

    assert transport.calls == []
