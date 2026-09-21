from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
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
            2026, 9, 21, 2, 10, self._tick, tzinfo=timezone.utc
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
                        "amount": -499,
                        "balance": 1501,
                        "itemClass": "UNKNOWN",
                        "itemClassData": {"source": "provider"},
                    }
                ],
                "moreAvailable": False,
            }
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _client(
    *,
    application_key: str = "live-key-123",
    account_id: str = "account-A",
) -> tuple[BetfairReadOnlyClient, _Transport]:
    transport = _Transport(application_key)
    return (
        BetfairReadOnlyClient(
            BetfairSessionCredentials(application_key, "session-secret"),
            transport=transport,
            clock=_Clock(),
            venue_id="betfair",
            account_id=account_id,
        ),
        transport,
    )


def test_captures_redacted_entitlement_statement_and_exact_request_scope() -> None:
    client, transport = _client()

    observation = read_betfair_provider_billing_inputs(
        client,
        statement_from="2026-09-01T00:00:00Z",
        statement_to="2026-09-21T00:00:00Z",
    )

    assert type(observation) is BetfairProviderBillingInputsObservation
    assert observation.entitlement.venue_id == "betfair"
    assert observation.entitlement.app_id == 41
    assert observation.entitlement.version_id == 7
    assert observation.entitlement.active is True
    assert observation.entitlement.delay_data is False
    assert observation.entitlement.vendor_id == "vendor-3"
    assert len(observation.entitlement.source_projection_sha256) == 64
    assert not hasattr(observation.entitlement, "application_key")
    assert not hasattr(observation.entitlement, "application_key_sha256")
    assert not hasattr(observation.entitlement, "evidence")
    assert "live-key-123" not in repr(observation)

    assert observation.statement.currency_code == "GBP"
    assert observation.statement.statement_from == "2026-09-01T00:00:00Z"
    assert observation.statement.statement_to == "2026-09-21T00:00:00Z"
    assert len(observation.statement.request_scope_sha256) == 64
    assert observation.statement.items[0].amount == Decimal("-499")
    assert observation.statement.items[0].item_class == "UNKNOWN"
    assert not hasattr(observation.entitlement, "account_id")
    assert not hasattr(observation.statement, "account_id")
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


def test_caller_account_label_cannot_become_authenticated_billing_identity() -> None:
    first_client, _ = _client(account_id="account-A")
    second_client, _ = _client(account_id="account-B")

    first = read_betfair_provider_billing_inputs(first_client)
    second = read_betfair_provider_billing_inputs(second_client)

    assert not hasattr(first.entitlement, "account_id")
    assert not hasattr(first.statement, "account_id")
    assert first.evidence_sha256 == second.evidence_sha256


def test_same_response_under_different_statement_scope_has_new_identity() -> None:
    first_client, _ = _client()
    second_client, _ = _client()

    first = read_betfair_provider_billing_inputs(
        first_client,
        from_record=0,
        record_count=10,
        statement_from="2026-09-01T00:00:00Z",
        statement_to="2026-09-10T00:00:00Z",
    )
    second = read_betfair_provider_billing_inputs(
        second_client,
        from_record=100,
        record_count=10,
        statement_from="2026-09-11T00:00:00Z",
        statement_to="2026-09-21T00:00:00Z",
    )

    assert first.statement.items == second.statement.items
    assert first.statement.statement_evidence == second.statement.statement_evidence
    assert first.statement.request_scope_sha256 != second.statement.request_scope_sha256
    assert first.evidence_sha256 != second.evidence_sha256


def test_more_available_changes_evidence_identity() -> None:
    complete_client, _ = _client()
    partial_client, partial_transport = _client()
    original_post = partial_transport.post

    def post(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        payload = original_post(
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        request = json.loads(body)
        if request["method"] != "AccountAPING/v1.0/getAccountStatement":
            return payload
        envelope = json.loads(payload)
        envelope["result"]["moreAvailable"] = True
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    partial_transport.post = post  # type: ignore[method-assign]
    complete = read_betfair_provider_billing_inputs(complete_client)
    partial = read_betfair_provider_billing_inputs(partial_client)

    assert complete.statement.more_available is False
    assert partial.statement.more_available is True
    assert complete.evidence_sha256 != partial.evidence_sha256


def test_rejects_statement_response_larger_than_requested_page() -> None:
    client, transport = _client()
    original_post = transport.post

    def post(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        payload = original_post(
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        request = json.loads(body)
        if request["method"] != "AccountAPING/v1.0/getAccountStatement":
            return payload
        envelope = json.loads(payload)
        row = envelope["result"]["accountStatement"][0]
        envelope["result"]["accountStatement"] = [
            row,
            {**row, "refId": "billing-ref-2"},
        ]
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    transport.post = post  # type: ignore[method-assign]
    with pytest.raises(BetfairReadOnlyError, match="exceeds requested record_count"):
        read_betfair_provider_billing_inputs(client, record_count=1)


def test_missing_statement_rows_are_absence_not_zero() -> None:
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
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"accountStatement": [], "moreAvailable": False},
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


def test_rejects_wrong_authenticated_application_key() -> None:
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
                        "message": "secret provider detail session-secret",
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
    assert "session-secret" not in str(exc_info.value)


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


def test_rejects_bool_response_id_duplicate_keys_and_string_money() -> None:
    client, transport = _client()
    original_post = transport.post

    def bool_id(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        if request["method"] == "AccountAPING/v1.0/getAccountDetails":
            return b'{"jsonrpc":"2.0","id":true,"result":{"currencyCode":"GBP"}}'
        return original_post(url, headers=headers, body=body, timeout_seconds=timeout_seconds)

    transport.post = bool_id  # type: ignore[method-assign]
    with pytest.raises(BetfairReadOnlyError, match="id does not match"):
        read_betfair_provider_billing_inputs(client)

    client, transport = _client()
    original_post = transport.post

    def duplicate(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        if request["method"] == "AccountAPING/v1.0/getAccountDetails":
            return b'{"jsonrpc":"2.0","id":1,"result":{"currencyCode":"GBP"},"result":{}}'
        return original_post(url, headers=headers, body=body, timeout_seconds=timeout_seconds)

    transport.post = duplicate  # type: ignore[method-assign]
    with pytest.raises(BetfairReadOnlyError, match="duplicate object key"):
        read_betfair_provider_billing_inputs(client)

    client, transport = _client()
    original_post = transport.post

    def string_money(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        payload = original_post(url, headers=headers, body=body, timeout_seconds=timeout_seconds)
        request = json.loads(body)
        if request["method"] != "AccountAPING/v1.0/getAccountStatement":
            return payload
        envelope = json.loads(payload)
        envelope["result"]["accountStatement"][0]["amount"] = "-499.00"
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    transport.post = string_money  # type: ignore[method-assign]
    with pytest.raises(BetfairReadOnlyError, match="JSON number decoded without binary float"):
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
