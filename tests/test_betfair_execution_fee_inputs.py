from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from threading import Event, Thread

import pytest

from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_execution_fee_inputs import read_betfair_execution_fee_inputs


FIXED_NOW = datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


class FirstCallGateTransport(FakeTransport):
    def __init__(self, responses: list[bytes]) -> None:
        super().__init__(responses)
        self.entered = Event()
        self.release = Event()

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        first = not self.calls
        if first:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise AssertionError("test did not release provider read gate")
        return super().post(
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )


def response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def account_details(*, discount_rate: object = 12.5) -> dict[str, object]:
    return {
        "currencyCode": "GBP",
        "localeCode": "en",
        "region": "GBR",
        "timezone": "Europe/London",
        "discountRate": discount_rate,
        "pointsBalance": 1234,
        "firstName": "DoNotPersist",
        "lastName": "DoNotPersist",
    }


def market_description() -> list[dict[str, object]]:
    return [
        {
            "marketId": "1.234",
            "description": {
                "marketBaseRate": 5.0,
                "discountAllowed": True,
                "regulator": "MR_INT",
            },
        }
    ]


def client_for(*responses: bytes) -> tuple[BetfairReadOnlyClient, FakeTransport]:
    transport = FakeTransport(list(responses))
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )
    return client, transport


def test_reads_authenticated_account_and_exact_market_fee_inputs_with_causal_evidence():
    account_raw = response(account_details(), 1)
    market_raw = response(market_description(), 2)
    client, transport = client_for(account_raw, market_raw)

    observation = read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert observation.venue_id == "betfair-exchange"
    assert observation.account_id == "account-123"
    assert observation.currency_code == "GBP"
    assert observation.region == "GBR"
    assert observation.market_id == "1.234"
    assert observation.discount_rate_percent == Decimal("12.5")
    assert observation.market_base_rate_percent == Decimal("5.0")
    assert observation.discount_allowed is True
    assert observation.regulator == "MR_INT"
    assert observation.account_evidence.observed_at == FIXED_NOW.isoformat()
    assert observation.account_evidence.source_payload_sha256 == sha256(account_raw).hexdigest()
    assert observation.market_evidence.source_payload_sha256 == sha256(market_raw).hexdigest()
    assert not hasattr(observation, "first_name")
    assert not hasattr(observation, "last_name")

    assert [call["url"] for call in transport.calls] == [
        ACCOUNT_JSON_RPC_ENDPOINT,
        BETTING_JSON_RPC_ENDPOINT,
    ]
    requests = [json.loads(call["body"]) for call in transport.calls]
    assert requests[0]["method"] == "AccountAPING/v1.0/getAccountDetails"
    assert requests[0]["params"] == {}
    assert requests[1]["method"] == "SportsAPING/v1.0/listMarketCatalogue"
    assert requests[1]["params"] == {
        "filter": {"marketIds": ["1.234"]},
        "marketProjection": ["MARKET_DESCRIPTION"],
        "maxResults": 1,
    }


def test_missing_discount_rate_fails_closed_before_market_lookup():
    details = account_details()
    del details["discountRate"]
    client, transport = client_for(response(details, 1))

    with pytest.raises(
        BetfairReadOnlyError,
        match="discount_rate_percent is missing from provider response",
    ):
        read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert len(transport.calls) == 1


@pytest.mark.parametrize("discount_rate", [-1, 101])
def test_out_of_range_provider_discount_rate_fails_closed(discount_rate: int):
    client, transport = client_for(response(account_details(discount_rate=discount_rate), 1))

    with pytest.raises(BetfairReadOnlyError, match="between 0 and 100"):
        read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert len(transport.calls) == 1


def test_market_lookup_requires_one_exact_matching_market():
    client, _ = client_for(
        response(account_details(discount_rate=0), 1),
        response(
            [
                {
                    "marketId": "1.999",
                    "description": {
                        "marketBaseRate": 5,
                        "discountAllowed": True,
                    },
                }
            ],
            2,
        ),
    )

    with pytest.raises(BetfairReadOnlyError, match="different market"):
        read_betfair_execution_fee_inputs(client, market_id="1.234")


def test_market_description_missing_or_malformed_fee_fields_fail_closed():
    account = response(account_details(discount_rate=0), 1)
    client, _ = client_for(
        account,
        response([{"marketId": "1.234", "description": {}}], 2),
    )
    with pytest.raises(
        BetfairReadOnlyError,
        match="market_base_rate_percent is missing from provider response",
    ):
        read_betfair_execution_fee_inputs(client, market_id="1.234")

    client, _ = client_for(
        account,
        response(
            [
                {
                    "marketId": "1.234",
                    "description": {
                        "marketBaseRate": 5,
                        "discountAllowed": "yes",
                    },
                }
            ],
            2,
        ),
    )
    with pytest.raises(BetfairReadOnlyError, match="discountAllowed must be bool"):
        read_betfair_execution_fee_inputs(client, market_id="1.234")


def test_optional_region_and_regulator_are_preserved_without_becoming_authority():
    details = account_details()
    details.pop("region")
    client, _ = client_for(
        response(details, 1),
        response(
            [
                {
                    "marketId": "1.234",
                    "description": {
                        "marketBaseRate": 5,
                        "discountAllowed": False,
                    },
                }
            ],
            2,
        ),
    )

    observation = read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert observation.region is None
    assert observation.regulator is None
    assert observation.discount_allowed is False


def test_subclass_cannot_override_provider_read_authority():
    class ForgedClient(BetfairReadOnlyClient):
        def _rpc(self, method, params):
            raise AssertionError("subclass override must not be invoked")

    transport = FakeTransport([])
    client = ForgedClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )

    with pytest.raises(TypeError, match="exact BetfairReadOnlyClient"):
        read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert transport.calls == []


@pytest.mark.parametrize(
    "method_name",
    ["_rpc", "_next_request_id", "_observed_at", "_redact_provider_message"],
)
def test_instance_shadow_of_read_capability_fails_before_transport(method_name: str):
    client, transport = client_for()
    setattr(client, method_name, lambda *args, **kwargs: None)

    with pytest.raises(BetfairReadOnlyError, match="instance-shadowed"):
        read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert transport.calls == []


def test_post_snapshot_identity_and_method_mutation_cannot_rebind_provider_evidence():
    account_raw = response(account_details(), 1)
    market_raw = response(market_description(), 2)
    transport = FirstCallGateTransport([account_raw, market_raw])
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )
    result: list[object] = []
    failures: list[BaseException] = []

    def read() -> None:
        try:
            result.append(read_betfair_execution_fee_inputs(client, market_id="1.234"))
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    worker = Thread(target=read)
    worker.start()
    assert transport.entered.wait(timeout=5), "provider read did not reach deterministic gate"

    # These mutations happen after snapshot/preflight while the first provider read
    # is blocked. A validate-then-use implementation can bind authentic payloads to
    # these forged identities or dynamically invoke the new method shadow.
    client._venue_id = "forged-venue"
    client._account_id = "forged-account"
    client._observed_at = lambda: "2099-01-01T00:00:00+00:00"
    client._next_request_id = lambda: 999
    transport.release.set()
    worker.join(timeout=5)

    assert not worker.is_alive(), "fee-input read did not finish after gate release"
    assert failures == []
    assert len(result) == 1
    observation = result[0]
    assert observation.venue_id == "betfair-exchange"
    assert observation.account_id == "account-123"
    assert observation.account_evidence.observed_at == FIXED_NOW.isoformat()
    assert observation.market_evidence.observed_at == FIXED_NOW.isoformat()
    assert [json.loads(call["body"])["id"] for call in transport.calls] == [1, 2]


def test_provider_callback_mutating_original_rpc_shadow_cannot_take_over_second_read():
    account_raw = response(account_details(), 1)
    market_raw = response(market_description(), 2)
    transport = FakeTransport([account_raw, market_raw])
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )
    original_post = transport.post
    calls = 0

    def mutating_post(url, *, headers, body, timeout_seconds):
        nonlocal calls
        calls += 1
        payload = original_post(
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        if calls == 1:
            client._rpc = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("post-snapshot rpc shadow must never be invoked")
            )
        return payload

    transport.post = mutating_post

    observation = read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert observation.account_id == "account-123"
    assert calls == 2
