from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)


class _StaticTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        return self.payload


def _client(error_code: object) -> BetfairReadOnlyClient:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": error_code,
                "message": "provider rejected request",
            },
        }
    ).encode("utf-8")
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=_StaticTransport(payload),
        clock=lambda: datetime(2026, 9, 17, 19, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize("error_code", ["app-secret/session-secret", True])
def test_provider_controlled_non_integer_error_code_fails_without_secret_echo(
    error_code: object,
) -> None:
    client = _client(error_code)

    with pytest.raises(BetfairReadOnlyError) as captured:
        client.read_account_funds()

    message = str(captured.value)
    assert message == "Betfair JSON-RPC returned a malformed error"
    assert "app-secret" not in message
    assert "session-secret" not in message


def test_integer_error_code_remains_safe_diagnostic_evidence() -> None:
    client = _client(-32000)

    with pytest.raises(BetfairReadOnlyError) as captured:
        client.read_account_funds()

    message = str(captured.value)
    assert "code=-32000" in message
    assert "app-secret" not in message
    assert "session-secret" not in message
