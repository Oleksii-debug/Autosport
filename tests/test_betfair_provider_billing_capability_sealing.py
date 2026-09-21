from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

import autosport.betfair_provider_billing_inputs as billing
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)


class _Clock:
    def __call__(self) -> datetime:
        return datetime(2026, 9, 21, 2, 5, tzinfo=timezone.utc)


class _MissingCurrencyTransport:
    def __init__(self, *, rebind_mid_call: bool) -> None:
        self._rebind_mid_call = rebind_mid_call
        self.calls = 0
        self.original_provider_text = billing._provider_text

    def post(
        self,
        _url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        assert headers["X-Application"] == "live-key"
        assert headers["X-Authentication"] == "session-token"
        assert timeout_seconds > 0
        request = json.loads(body)
        self.calls += 1
        if self._rebind_mid_call and self.calls == 1:
            billing._provider_text = lambda *_args, **_kwargs: "GBP"  # type: ignore[assignment]
        assert request["method"] == "AccountAPING/v1.0/getAccountDetails"
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": {}},
            separators=(",", ":"),
        ).encode("utf-8")


def _client(transport: _MissingCurrencyTransport) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("live-key", "session-token"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="caller-only-label",
    )


def test_pre_call_module_rebind_cannot_replace_captured_parser() -> None:
    original = billing._provider_text
    transport = _MissingCurrencyTransport(rebind_mid_call=False)
    billing._provider_text = lambda *_args, **_kwargs: "GBP"  # type: ignore[assignment]
    try:
        with pytest.raises(
            BetfairReadOnlyError,
            match="currency_code is missing from provider response",
        ):
            billing.read_betfair_provider_billing_inputs(_client(transport))
    finally:
        billing._provider_text = original

    assert transport.calls == 1


def test_provider_callback_cannot_rebind_parser_mid_capture() -> None:
    transport = _MissingCurrencyTransport(rebind_mid_call=True)
    try:
        with pytest.raises(
            BetfairReadOnlyError,
            match="currency_code is missing from provider response",
        ):
            billing.read_betfair_provider_billing_inputs(_client(transport))
    finally:
        billing._provider_text = transport.original_provider_text

    assert transport.calls == 1


def test_pre_call_module_rebind_cannot_replace_rpc_or_decoder() -> None:
    original_rpc = billing._read_rpc
    original_decode = billing._decode_json
    attacker_called = {"rpc": False, "decode": False}

    def attacker_rpc(*_args, **_kwargs):
        attacker_called["rpc"] = True
        raise AssertionError("module mirror RPC must not execute")

    def attacker_decode(*_args, **_kwargs):
        attacker_called["decode"] = True
        raise AssertionError("module mirror decoder must not execute")

    billing._read_rpc = attacker_rpc  # type: ignore[assignment]
    billing._decode_json = attacker_decode  # type: ignore[assignment]
    transport = _MissingCurrencyTransport(rebind_mid_call=False)
    try:
        with pytest.raises(
            BetfairReadOnlyError,
            match="currency_code is missing from provider response",
        ):
            billing.read_betfair_provider_billing_inputs(_client(transport))
    finally:
        billing._read_rpc = original_rpc
        billing._decode_json = original_decode

    assert attacker_called == {"rpc": False, "decode": False}
    assert transport.calls == 1
