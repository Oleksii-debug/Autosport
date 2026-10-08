"""Plan 4 Section 2: fail-closed Betfair read-only transport error boundary.

Only synthetic credentials, transport mocks, and read-only methods are used.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)

_APP = "synthetic-app-marker-not-a-credential"
_SESSION = "synthetic-session-marker-not-a-credential"
_NOW = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)


class _Transport:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls += 1
        assert headers["X-Application"] == _APP
        assert headers["X-Authentication"] == _SESSION
        if self.error is not None:
            raise self.error
        return b'{"jsonrpc":"2.0","result":{"currencyCode":"EUR"},"id":2}'


@pytest.mark.parametrize("exception_type", [RuntimeError, OSError, BetfairReadOnlyError])
def test_transport_failure_is_redacted_and_never_retried(exception_type):
    # Hostile transports may accidentally echo either secret from their request.
    transport = _Transport(exception_type(
        "transport leaked " + _APP + " / " + _SESSION
    ))
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials(_APP, _SESSION),
        transport=transport,
        clock=lambda: _NOW,
    )

    with pytest.raises(BetfairReadOnlyError, match="read-only transport failed") as raised:
        client.read_account_details()

    assert transport.calls == 1
    assert raised.value.__context__ is None  # no retained secret-bearing exception
    assert raised.value.__cause__ is None
    assert _APP not in str(raised.value)
    assert _SESSION not in str(raised.value)
    assert _APP not in repr(raised.value)
    assert _SESSION not in repr(raised.value)

    # Failed reads neither publish account truth nor become an implicit retry.
    # A subsequent *explicit* read may recover using a new provider response.
    transport.error = None
    details = client.read_account_details()
    assert transport.calls == 2
    assert details.currency_code == "EUR"
