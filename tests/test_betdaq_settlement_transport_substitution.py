from __future__ import annotations

import pytest

import test_betdaq_settlement_readback as settlement_tests
from autosport.betdaq_settlement_readback import BetdaqEconomicReadbackError


class _ForgedTransport:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.calls = 0

    def post(self, *args, **kwargs) -> bytes:
        self.calls += 1
        return self._payload


class _SwapTransportOnEnter:
    def __init__(self, account, replacement) -> None:
        self._account = account
        self._replacement = replacement

    def __enter__(self):
        self._account._transport = self._replacement
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_transport_substitution_at_call_lock_boundary_cannot_mint_economic_evidence(
    monkeypatch,
) -> None:
    """The transport actually used must be revalidated after call-lock admission."""

    client, canonical_opener = settlement_tests.economic_client(monkeypatch)
    account = client._account_client
    forged = _ForgedTransport(
        settlement_tests.postings_by_id(settlement_tests.posting(9001))
    )
    account._call_lock = _SwapTransportOnEnter(account, forged)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic evidence requires product-owned HTTPS transport",
    ):
        client.read_account_postings_by_id(9001)

    assert forged.calls == 0
    assert canonical_opener.calls == []
