from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqAccountReadOnlyError,
    BetdaqCredentials,
)


_EXTERNAL_NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"


class _StaticTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls += 1
        return self.payload


def _clock() -> datetime:
    return datetime(2026, 9, 22, 21, 1, tzinfo=timezone.utc)


def _balance_response(return_status: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{_SOAP_NS}" xmlns="{_EXTERNAL_NS}">'
        "<soap:Body>"
        "<GetAccountBalancesResponse>"
        '<GetAccountBalancesResult Currency="EUR" Balance="120.02" '
        'Exposure="-20.01" AvailableFunds="100.01" Credit="0">'
        f"{return_status}"
        "</GetAccountBalancesResult>"
        "</GetAccountBalancesResponse>"
        "</soap:Body>"
        "</soap:Envelope>"
    ).encode("utf-8")


def _client(payload: bytes) -> tuple[BetdaqAccountReadOnlyClient, _StaticTransport]:
    transport = _StaticTransport(payload)
    return (
        BetdaqAccountReadOnlyClient(
            BetdaqCredentials("alice", "secret-pass", "app-id"),
            transport=transport,
            clock=_clock,
            account_id="acct",
        ),
        transport,
    )


def test_base_return_status_success_remains_valid_balance_evidence() -> None:
    client, transport = _client(
        _balance_response(
            '<ReturnStatus Code="0" Description="Success" CallId="call-success" />'
        )
    )

    balance = client.read_account_balance()

    assert transport.calls == 1
    assert balance.currency == "EUR"
    assert balance.available_funds == Decimal("100.01")


def test_non_success_base_return_status_cannot_become_positive_balance_evidence() -> None:
    client, transport = _client(
        _balance_response(
            '<ReturnStatus Code="1" Description="Provider-declared failure" '
            'CallId="call-failure" />'
        )
    )

    with pytest.raises(BetdaqAccountReadOnlyError):
        client.read_account_balance()

    assert transport.calls == 1


def test_missing_base_return_status_fails_closed() -> None:
    client, transport = _client(_balance_response(""))

    with pytest.raises(BetdaqAccountReadOnlyError):
        client.read_account_balance()

    assert transport.calls == 1
