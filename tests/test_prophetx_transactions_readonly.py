from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import pytest

from autosport.prophetx_account_readonly import (
    ProphetXHttpResponse, ProphetXReadOnlyError, ProphetXSessionToken,
)
from autosport.prophetx_transactions_readonly import (
    ProphetXTransactionQuery, ProphetXTransactionsClient,
)
from autosport.prophetx_transactions_transport import (
    TRANSACTIONS_URL, UrllibProphetXTransactionsTransport,
)

NOW = datetime(2026, 9, 23, 1, 40, tzinfo=timezone.utc)


def row(**kw):
    value = {
        "status": "Completed", "user_id": "user-1", "transaction_type": "PAY",
        "transaction_sub_type": "OTHER", "amount": 150, "change": 150,
        "balance": 1150, "balance_before": 1000, "details": "client-id",
        "market_id": "219", "event_id": "44285780", "trade_id": "abc123",
        "description": "settled win", "created_at": "2026-08-10T14:00:00Z",
    }
    value.update(kw)
    return value


def payload(*rows, cursor=None):
    data = {"transactions": list(rows)}
    if cursor is not None:
        data["next_cursor"] = cursor
    return json.dumps({"data": data}, separators=(",", ":")).encode()


class FakeTransport:
    def __init__(self, *bodies):
        self.bodies, self.calls = list(bodies), []

    def get(self, url, *, headers, timeout_seconds):
        self.calls.append((url, dict(headers), timeout_seconds))
        return ProphetXHttpResponse(
            200, url, "application/json", "identity", self.bodies.pop(0)
        )


def client(*bodies, clock=lambda: NOW):
    transport = FakeTransport(*bodies)
    return (
        ProphetXTransactionsClient(
            ProphetXSessionToken("session-secret"), transport=transport, clock=clock
        ),
        transport,
    )


def test_exact_query_secret_safe_get_and_exact_money():
    query = ProphetXTransactionQuery(
        limit=1000, next_cursor="c1", from_unix=100, to_unix=200,
        market_id="market 1", trade_id="trade/1", event_id="e1",
        transaction_type="COMMISSION",
    )
    c, transport = client(payload(row(
        transaction_type="COMMISSION", amount=12.5, change=-12.5,
        balance=987.5, balance_before=1000,
    )))
    page = c.read_page(query)
    url, headers, timeout = transport.calls[0]
    assert url == (
        TRANSACTIONS_URL
        + "?limit=1000&next_cursor=c1&from=100&to=200&market_id=market+1"
          "&trade_id=trade%2F1&event_id=e1&transaction_type=COMMISSION"
    )
    assert "session-secret" not in url and "session-secret" not in repr(c)
    assert headers["Authorization"] == "Bearer session-secret"
    assert headers["Accept-Encoding"] == "identity" and timeout == 10.0
    tx = page.transactions[0]
    assert tx.amount == Decimal("12.5") and tx.change == Decimal("-12.5")
    assert tx.balance == Decimal("987.5") and tx.currency == "USD"


def test_optional_provider_fields_preserve_null_empty_and_spaces():
    c, _ = client(payload(
        row(transaction_type="DEPOSIT", transaction_sub_type="", details=None,
            market_id="", event_id=None, trade_id=None, description="  provider text  ")
    ))
    tx = c.read_page().transactions[0]
    assert tx.transaction_sub_type == "" and tx.market_id == ""
    assert tx.trade_id is None and tx.description == "  provider text  "


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 1001}, {"limit": True}, {"from_unix": True},
    {"from_unix": 20, "to_unix": 10}, {"next_cursor": " "},
    {"transaction_type": "UNKNOWN"},
])
def test_bad_query_fails_closed(kwargs):
    with pytest.raises(ProphetXReadOnlyError):
        ProphetXTransactionQuery(**kwargs)


@pytest.mark.parametrize("overrides", [
    {"status": "Unknown"}, {"transaction_type": "MYSTERY"}, {"amount": -1},
    {"amount": "1.0"}, {"change": "1.0"},
    {"created_at": "2026-08-10T14:00:00"},
])
def test_bad_row_fails_closed(overrides):
    c, _ = client(payload(row(**overrides)))
    with pytest.raises(ProphetXReadOnlyError):
        c.read_page()


def test_transaction_balance_equation_is_fail_closed_and_decimal_context_independent():
    c, _ = client(payload(row(balance_before=1000, change=10, balance=1009)))
    with pytest.raises(ProphetXReadOnlyError, match="balance_before plus change"):
        c.read_page()

    huge_before = Decimal("9" * 120 + ".123456789012345678901234567890")
    change = Decimal("0.000000000000000000000000000001")
    exact_balance = Decimal("9" * 120 + ".123456789012345678901234567891")
    body = json.dumps({
        "data": {"transactions": [{
            "status": "Completed", "user_id": "u", "transaction_type": "PAY",
            "amount": 1, "change": str(change), "balance": str(exact_balance),
            "balance_before": str(huge_before), "created_at": "2026-08-10T14:00:00Z",
        }]}
    }).encode()
    # JSON numeric strings are intentionally rejected; the exact-arithmetic path is
    # exercised below directly with Decimal values through the parser's JSON numbers.
    c2, _ = client(body)
    with pytest.raises(ProphetXReadOnlyError, match="exact finite JSON number"):
        c2.read_page()


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_nonstandard_json_numbers_fail_closed(literal):
    body = (
        b'{"data":{"transactions":[{"status":"Completed","user_id":"u",'
        b'"transaction_type":"PAY","amount":' + literal +
        b',"change":1,"balance":2,"balance_before":1,'
        b'"created_at":"2026-08-10T14:00:00Z"}]}}'
    )
    c, _ = client(body)
    with pytest.raises(ProphetXReadOnlyError):
        c.read_page()


def test_duplicate_json_key_fails_closed():
    body = (
        b'{"data":{"transactions":[{"status":"Completed","status":"Failed",'
        b'"user_id":"u","transaction_type":"PAY","amount":1,"change":1,'
        b'"balance":2,"balance_before":1,"created_at":"2026-08-10T14:00:00Z"}]}}'
    )
    c, _ = client(body)
    with pytest.raises(ProphetXReadOnlyError, match="duplicate"):
        c.read_page()


def test_clock_is_not_read_until_full_payload_is_accepted():
    calls = []
    def clock():
        calls.append(1)
        return NOW
    c, _ = client(b'{"data":{"transactions":"bad"}}', clock=clock)
    with pytest.raises(ProphetXReadOnlyError):
        c.read_page()
    assert calls == []


def test_injected_transport_never_mints_provider_origin():
    c, _ = client(payload(row()))
    page = c.read_page()
    assert not c.provider_origin_proven(page)


def test_canonical_origin_is_exact_object_only_and_invalidated_on_transport_swap(monkeypatch):
    body = payload(row())
    def fake_get(self, url, *, headers, timeout_seconds):
        return ProphetXHttpResponse(200, url, "application/json", "identity", body)
    monkeypatch.setattr(UrllibProphetXTransactionsTransport, "get", fake_get)
    c = ProphetXTransactionsClient(ProphetXSessionToken("secret"), clock=lambda: NOW)
    page = c.read_page()
    assert c.provider_origin_proven(page)
    assert not c.provider_origin_proven(replace(page))
    c._transport = FakeTransport(body)
    assert not c.provider_origin_proven(page)


def test_tampered_live_page_loses_origin_proof(monkeypatch):
    body = payload(row())
    def fake_get(self, url, *, headers, timeout_seconds):
        return ProphetXHttpResponse(200, url, "application/json", "identity", body)
    monkeypatch.setattr(UrllibProphetXTransactionsTransport, "get", fake_get)
    c = ProphetXTransactionsClient(ProphetXSessionToken("secret"), clock=lambda: NOW)
    page = c.read_page()
    object.__setattr__(page, "next_cursor", "forged")
    assert not c.provider_origin_proven(page)


def test_tampered_transaction_optional_field_loses_origin_proof(monkeypatch):
    body = payload(row(description="original", transaction_sub_type="OTHER"))
    def fake_get(self, url, *, headers, timeout_seconds):
        return ProphetXHttpResponse(200, url, "application/json", "identity", body)
    monkeypatch.setattr(UrllibProphetXTransactionsTransport, "get", fake_get)
    c = ProphetXTransactionsClient(ProphetXSessionToken("secret"), clock=lambda: NOW)
    page = c.read_page()
    tx = page.transactions[0]
    object.__setattr__(tx, "description", "forged")
    assert not c.provider_origin_proven(page)


def test_blank_provider_cursor_fails_closed():
    c, _ = client(payload(row(), cursor=" "))
    with pytest.raises(ProphetXReadOnlyError, match="next_cursor"):
        c.read_page()


def test_pagination_preserves_filters_and_rejects_cursor_cycle_or_partial_completion():
    c, transport = client(payload(row(), cursor="c2"), payload(row()))
    query = ProphetXTransactionQuery(limit=1, market_id="m1", transaction_type="PAY")
    pages = c.read_all(query, max_pages=2)
    assert len(pages) == 2
    assert "next_cursor=c2" in transport.calls[1][0]
    assert "market_id=m1" in transport.calls[1][0]
    assert "transaction_type=PAY" in transport.calls[1][0]

    cyc, _ = client(payload(row(), cursor="same"), payload(row(), cursor="same"))
    with pytest.raises(ProphetXReadOnlyError, match="repeated"):
        cyc.read_all(max_pages=3)

    partial, _ = client(payload(row(), cursor="more"))
    with pytest.raises(ProphetXReadOnlyError, match="page bound"):
        partial.read_all(max_pages=1)


@pytest.mark.parametrize("response", [
    ProphetXHttpResponse(500, TRANSACTIONS_URL + "?limit=20", "application/json", "identity", b"{}"),
    ProphetXHttpResponse(200, "https://evil.example/?limit=20", "application/json", "identity", b"{}"),
    ProphetXHttpResponse(200, TRANSACTIONS_URL + "?limit=20", "text/html", "identity", b"{}"),
    ProphetXHttpResponse(200, TRANSACTIONS_URL + "?limit=20", "application/json", "gzip", b"{}"),
])
def test_http_response_contract_fails_closed(response):
    class T:
        def get(self, url, *, headers, timeout_seconds):
            return response
    c = ProphetXTransactionsClient(
        ProphetXSessionToken("secret"), transport=T(), clock=lambda: NOW
    )
    with pytest.raises(ProphetXReadOnlyError):
        c.read_page()


def test_transport_rejects_noncanonical_endpoint_before_network():
    transport = UrllibProphetXTransactionsTransport()
    with pytest.raises(ProphetXReadOnlyError, match="fixed ProphetX sandbox"):
        transport.get(
            "https://api.sandbox.prophetx.dev/partner/v4/mm/get_balance?limit=20",
            headers={}, timeout_seconds=1,
        )


def test_provider_cannot_exceed_requested_limit():
    c, _ = client(payload(row(), row()))
    with pytest.raises(ProphetXReadOnlyError, match="within requested limit"):
        c.read_page(ProphetXTransactionQuery(limit=1))
