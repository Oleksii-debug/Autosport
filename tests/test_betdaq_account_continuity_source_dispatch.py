from __future__ import annotations

from datetime import datetime, timezone

import pytest

import autosport.betdaq_account_continuity as continuity_module
import autosport.betdaq_account_readonly as account_module
from autosport.betdaq_account_continuity import (
    BetdaqAccountContinuityClient,
    BetdaqAccountContinuityError,
)
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqAccountReadOnlyError,
    BetdaqCredentials,
)
from autosport.bookmaker_capability import BookmakerCapability


SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"


class _FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._payload


class _QueueUrlopen:
    def __init__(self, *payloads: bytes) -> None:
        self._payloads = list(payloads)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self._payloads:
            raise AssertionError("unexpected urlopen call")
        return _FakeHttpResponse(self._payloads.pop(0))


def _balance_response() -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP}" xmlns="{NS}">'
        "<soap:Body><GetAccountBalancesResponse><GetAccountBalancesResult "
        'Currency="EUR" Balance="120.00" Exposure="-20.00" '
        'AvailableFunds="100.00" Credit="0">'
        '<ReturnStatus Code="0" Description="ok" CallId="fixture-call" />'
        "</GetAccountBalancesResult></GetAccountBalancesResponse>"
        "</soap:Body></soap:Envelope>"
    ).encode()


def _clock() -> datetime:
    return datetime(2026, 9, 23, 19, 30, tzinfo=timezone.utc)


def test_redirected_source_client_cannot_authenticate_b_and_issue_principal_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuity truth must bind to the exact canonical source constructor."""
    opener = _QueueUrlopen(_balance_response())
    monkeypatch.setattr(account_module, "urlopen", opener)

    principal_a = BetdaqCredentials("principal-a", "password-a", "application-a")
    principal_b = BetdaqCredentials("principal-b", "password-b", "application-b")
    canonical_source_class = BetdaqAccountReadOnlyClient

    def redirected_source(_credentials, **kwargs):
        return canonical_source_class(principal_b, **kwargs)

    monkeypatch.setattr(
        continuity_module,
        "BetdaqAccountReadOnlyClient",
        redirected_source,
    )

    with pytest.raises(
        (BetdaqAccountContinuityError, BetdaqAccountReadOnlyError),
    ):
        client = BetdaqAccountContinuityClient(principal_a, clock=_clock)
        client.read_account_evidence(
            frozenset({BookmakerCapability.BALANCE_READ})
        )


def test_shadowed_source_read_method_cannot_issue_continuity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance-level read shadow cannot become the authentication authority."""
    opener = _QueueUrlopen(_balance_response())
    monkeypatch.setattr(account_module, "urlopen", opener)

    client = BetdaqAccountContinuityClient(
        BetdaqCredentials("principal-a", "password-a", "application-a"),
        clock=_clock,
    )
    monkeypatch.setattr(
        client._source,
        "read_account_evidence",
        lambda _requested_capabilities: None,
    )

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="read authority was shadowed",
    ):
        client.read_account_evidence(
            frozenset({BookmakerCapability.BALANCE_READ})
        )
    assert opener.calls == []
