from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import urllib.request as _urllib_request

import pytest

from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.betdaq_settlement_readback import (
    BetdaqEconomicReadbackClient,
    BetdaqEconomicReadbackError,
    coalesce_posting_replays,
)


NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class _FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit=None):
        if limit is None:
            return self.payload
        return self.payload[:limit]

    def info(self):
        return {}


class _OneResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def __call__(self, request, *, timeout):
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("unexpected extra BETDAQ HTTPS dispatch")
        response = _FakeHttpResponse(self.payload)
        response.code = 200
        response.msg = "OK"
        return response


def _install_https(monkeypatch, responder: _OneResponse) -> None:
    def fake_do_open(_self, _http_class, request, **_kwargs):
        return responder(
            request,
            timeout=getattr(request, "timeout", 0),
        )

    monkeypatch.setattr(
        _urllib_request.AbstractHTTPHandler,
        "do_open",
        fake_do_open,
    )


def _postings_by_id_payload() -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP}" xmlns="{NS}">'
        '<soap:Body><ListAccountPostingsByIdResponse>'
        '<ListAccountPostingsByIdResult Currency="EUR" '
        'AvailableFunds="100.00" Balance="120.00" Credit="0" Exposure="-20.00">'
        '<ReturnStatus Code="0" Description="fixture-status" CallId="fixture-call" />'
        '<Orders><Order PostedAt="2026-09-22T23:59:00Z" '
        'Description="fixture" Amount="2.50" ResultingBalance="102.50" '
        'PostingCategory="7" OrderId="123" MarketId="200" '
        'TransactionId="9001" /></Orders>'
        '</ListAccountPostingsByIdResult></ListAccountPostingsByIdResponse>'
        '</soap:Body></soap:Envelope>'
    ).encode()


def _issued_readback(monkeypatch):
    responder = _OneResponse(_postings_by_id_payload())
    _install_https(monkeypatch, responder)
    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=lambda: datetime(2026, 9, 23, 0, 10, tzinfo=timezone.utc),
    )
    client = BetdaqEconomicReadbackClient(account)
    value = client.read_account_postings_by_id(9000)
    assert responder.calls == 1
    return value


def test_issued_posting_slots_are_write_once_after_validation(monkeypatch) -> None:
    value = _issued_readback(monkeypatch)
    row = value.postings[0]

    with pytest.raises(BetdaqEconomicReadbackError, match="field amount is write-once"):
        object.__setattr__(row, "amount", Decimal("9.99"))

    assert row.amount == Decimal("2.50")
    assert coalesce_posting_replays(value) == (row,)


def test_valid_value_copy_cannot_rebind_old_provider_payload_to_new_economics(
    monkeypatch,
) -> None:
    value = _issued_readback(monkeypatch)
    row = value.postings[0]
    forged_row = replace(row, amount=Decimal("9.99"))
    forged = replace(value, postings=(forged_row,))

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="posting observation was not issued from canonical provider bytes",
    ):
        _ = forged_row.observation_id

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="postings readback was not issued from canonical provider bytes",
    ):
        _ = forged.readback_id

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="postings readback was not issued from canonical provider bytes",
    ):
        coalesce_posting_replays(forged)


def test_exact_payload_replay_copy_preserves_stable_economic_identity(monkeypatch) -> None:
    value = _issued_readback(monkeypatch)
    row = value.postings[0]
    shifted_evidence = replace(
        value.evidence,
        observed_at="2026-09-23T00:20:00Z",
    )
    shifted_row = replace(row, evidence=shifted_evidence)
    shifted = replace(
        value,
        evidence=shifted_evidence,
        postings=(shifted_row,),
    )

    assert shifted.evidence.evidence_id == value.evidence.evidence_id
    assert shifted.evidence.acquisition_id != value.evidence.acquisition_id
    assert shifted_row.observation_id == row.observation_id
    assert shifted.readback_id == value.readback_id
    assert coalesce_posting_replays(shifted) == (shifted_row,)
