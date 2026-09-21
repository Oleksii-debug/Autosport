from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_read_completeness import (
    BetfairReadCompleteness,
    acquire_cleared_orders,
    acquire_current_orders,
    classify_betfair_read_failure,
)


FIXED_NOW = datetime(2026, 9, 21, 18, 55, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.value = FIXED_NOW

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=1)
        return current


class FakeTransport:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def error_response(message: str, request_id: int) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "error": {"code": -32099, "message": message},
            "id": request_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def current_order(bet_id: str, *, size_remaining: float = 1.0) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.123",
        "selectionId": 10,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T18:00:00+00:00",
        "priceSize": {"price": 2.0, "size": 1.0},
        "averagePriceMatched": 0,
        "sizeMatched": 0,
        "sizeRemaining": size_remaining,
        "customerOrderRef": "ord-1",
    }


def cleared_order(bet_id: str) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.123",
        "selectionId": 10,
        "side": "BACK",
        "placedDate": "2026-09-21T17:00:00+00:00",
        "settledDate": "2026-09-21T18:00:00+00:00",
        "priceRequested": 2.0,
        "priceMatched": 2.0,
        "sizeSettled": 1.0,
        "profit": 1.0,
        "customerOrderRef": "ord-1",
    }


def client_for(*responses: bytes | Exception) -> tuple[BetfairReadOnlyClient, FakeTransport]:
    transport = FakeTransport(list(responses))
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
    )
    return client, transport


def test_complete_empty_current_orders_is_authoritative_only_at_provider_end():
    client, _ = client_for(response({"currentOrders": [], "moreAvailable": False}, 1))

    acquisition = acquire_current_orders(
        client,
        market_ids=("1.123",),
        clock=FakeClock(),
    )

    assert acquisition.completeness is BetfairReadCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
    assert acquisition.failure_class is None
    assert acquisition.orders == ()
    assert acquisition.is_authoritative_empty() is True
    assert acquisition.require_complete() == ()


def test_timeout_after_valid_page_remains_partial_not_empty_or_complete():
    client, _ = client_for(
        response({"currentOrders": [current_order("bet-1")], "moreAvailable": True}, 1),
        BetfairReadOnlyError("Betfair network request failed"),
    )

    acquisition = acquire_current_orders(client, page_size=1, clock=FakeClock())

    assert acquisition.completeness is BetfairReadCompleteness.PARTIAL
    assert acquisition.failure_class is BetfairReadCompleteness.UNAVAILABLE_TRANSIENT
    assert [order.bet_id for order in acquisition.orders] == ["bet-1"]
    assert len(acquisition.pages) == 1
    assert acquisition.is_authoritative_empty() is False
    with pytest.raises(BetfairReadOnlyError, match="not complete"):
        acquisition.require_complete()


def test_too_many_requests_before_any_page_is_transient_not_empty():
    client, _ = client_for(error_response("TOO_MANY_REQUESTS", 1))

    acquisition = acquire_current_orders(client, clock=FakeClock())

    assert acquisition.completeness is BetfairReadCompleteness.UNAVAILABLE_TRANSIENT
    assert acquisition.failure_class is BetfairReadCompleteness.UNAVAILABLE_TRANSIENT
    assert acquisition.pages == ()
    assert acquisition.orders == ()
    assert acquisition.is_authoritative_empty() is False


def test_invalid_session_is_explicit_auth_degradation():
    client, _ = client_for(error_response("INVALID_SESSION_INFORMATION", 1))

    acquisition = acquire_current_orders(client, clock=FakeClock())

    assert acquisition.completeness is BetfairReadCompleteness.AUTH_INVALID_OR_EXPIRED
    assert acquisition.failure_class is BetfairReadCompleteness.AUTH_INVALID_OR_EXPIRED
    assert acquisition.is_authoritative_empty() is False


def test_malformed_provider_response_is_contract_failure_not_empty():
    client, _ = client_for(b"not-json")

    acquisition = acquire_current_orders(client, clock=FakeClock())

    assert acquisition.completeness is BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT
    assert acquisition.failure_class is BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT
    assert acquisition.is_authoritative_empty() is False


def test_more_available_empty_page_is_partial_and_never_complete():
    client, _ = client_for(response({"currentOrders": [], "moreAvailable": True}, 1))

    acquisition = acquire_current_orders(client, clock=FakeClock())

    assert acquisition.completeness is BetfairReadCompleteness.PARTIAL
    assert acquisition.failure_class is BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT
    assert len(acquisition.pages) == 1
    assert acquisition.is_authoritative_empty() is False


def test_max_pages_exhaustion_keeps_prefix_partial():
    client, transport = client_for(
        response({"currentOrders": [current_order("bet-1")], "moreAvailable": True}, 1)
    )

    acquisition = acquire_current_orders(
        client,
        page_size=1,
        max_pages=1,
        clock=FakeClock(),
    )

    assert acquisition.completeness is BetfairReadCompleteness.PARTIAL
    assert acquisition.failure_class is BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT
    assert [order.bet_id for order in acquisition.orders] == ["bet-1"]
    assert len(transport.calls) == 1


def test_exact_overlap_dedupes_but_conflicting_overlap_fails_closed():
    duplicate = current_order("bet-1")
    complete_client, _ = client_for(
        response({"currentOrders": [duplicate], "moreAvailable": True}, 1),
        response({"currentOrders": [duplicate], "moreAvailable": False}, 2),
    )
    complete = acquire_current_orders(complete_client, page_size=1, clock=FakeClock())
    assert complete.completeness is BetfairReadCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
    assert [order.bet_id for order in complete.orders] == ["bet-1"]

    conflict_client, _ = client_for(
        response({"currentOrders": [duplicate], "moreAvailable": True}, 1),
        response(
            {
                "currentOrders": [current_order("bet-1", size_remaining=0.5)],
                "moreAvailable": False,
            },
            2,
        ),
    )
    conflict = acquire_current_orders(conflict_client, page_size=1, clock=FakeClock())
    assert conflict.completeness is BetfairReadCompleteness.PARTIAL
    assert conflict.failure_class is BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT
    assert [order.bet_id for order in conflict.orders] == ["bet-1"]
    assert len(conflict.pages) == 1


def test_query_identity_binds_filters_and_cleared_window_semantics():
    first, first_transport = client_for(response({"currentOrders": [], "moreAvailable": False}, 1))
    second, _ = client_for(response({"currentOrders": [], "moreAvailable": False}, 1))
    q1 = acquire_current_orders(
        first,
        market_ids=("1.123",),
        customer_order_refs=("ord-1",),
        clock=FakeClock(),
    )
    q2 = acquire_current_orders(
        second,
        market_ids=("1.124",),
        customer_order_refs=("ord-1",),
        clock=FakeClock(),
    )
    assert q1.query_sha256 != q2.query_sha256
    request = json.loads(first_transport.calls[0]["body"])
    assert request["params"]["marketIds"] == ["1.123"]
    assert request["params"]["customerOrderRefs"] == ["ord-1"]

    cleared_a, _ = client_for(
        response({"clearedOrders": [cleared_order("bet-2")], "moreAvailable": False}, 1)
    )
    cleared_b, _ = client_for(
        response({"clearedOrders": [cleared_order("bet-2")], "moreAvailable": False}, 1)
    )
    a = acquire_cleared_orders(
        cleared_a,
        settled_from="2026-09-20T00:00:00+00:00",
        bet_status="SETTLED",
        clock=FakeClock(),
    )
    b = acquire_cleared_orders(
        cleared_b,
        settled_from="2026-09-21T00:00:00+00:00",
        bet_status="SETTLED",
        clock=FakeClock(),
    )
    assert a.query_sha256 != b.query_sha256


def test_caller_copy_cannot_mint_authoritative_complete_evidence():
    client, _ = client_for(response({"currentOrders": [], "moreAvailable": False}, 1))
    acquisition = acquire_current_orders(client, clock=FakeClock())
    copied = replace(acquisition)

    with pytest.raises(BetfairReadOnlyError, match="not issued"):
        copied.assert_authoritative()
    with pytest.raises(BetfairReadOnlyError, match="not issued"):
        copied.is_authoritative_empty()


def test_integrity_tamper_is_rejected_before_origin_check():
    client, _ = client_for(response({"currentOrders": [], "moreAvailable": False}, 1))
    acquisition = acquire_current_orders(client, clock=FakeClock())

    with pytest.raises(BetfairReadOnlyError, match="evidence digest mismatch"):
        replace(acquisition, query_sha256="0" * 64)


def test_failure_classifier_is_fail_closed_for_unknown_and_explicit_for_provider_states():
    assert classify_betfair_read_failure(
        BetfairReadOnlyError("Betfair HTTP request failed with status 503")
    ) is BetfairReadCompleteness.UNAVAILABLE_TRANSIENT
    assert classify_betfair_read_failure(
        BetfairReadOnlyError("Betfair JSON-RPC returned an error message=UNEXPECTED_ERROR")
    ) is BetfairReadCompleteness.UNAVAILABLE_TRANSIENT
    assert classify_betfair_read_failure(
        BetfairReadOnlyError("Betfair HTTP request failed with status 403")
    ) is BetfairReadCompleteness.AUTH_INVALID_OR_EXPIRED
    assert classify_betfair_read_failure(
        BetfairReadOnlyError("Betfair response id does not match request id")
    ) is BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT
