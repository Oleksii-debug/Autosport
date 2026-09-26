from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.bookmaker_capability import BookmakerCapability
from autosport.prophetx_account_readonly import (
    BALANCE_URL,
    ProphetXHttpResponse,
    ProphetXReadOnlyClient,
    ProphetXReadOnlyError,
    ProphetXSessionToken,
)


GOOD_BODY = (
    b'{"data":{"balance":1000.00,"gec_balance":500.00,'
    b'"matched_order_balance":200.00,"unmatched_order_balance":50.00,'
    b'"unmatched_order_balance_status":"succeed",'
    b'"unmatched_order_last_synced_at":"2026-08-10T14:12:40.307908108Z"}}'
)


class CallerSuppliedTransport:
    """A caller-controlled transport that performs no ProphetX network I/O."""

    def get(self, url: str, *, headers, timeout_seconds: float) -> ProphetXHttpResponse:
        assert url == BALANCE_URL
        return ProphetXHttpResponse(
            status=200,
            final_url=BALANCE_URL,
            content_type="application/json",
            content_encoding=None,
            body=GOOD_BODY,
        )


def test_caller_supplied_transport_cannot_mint_canonical_wallet_authority() -> None:
    """Synthetic bytes may exercise parsing, but must not prove provider-origin evidence."""

    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("synthetic-session"),
        transport=CallerSuppliedTransport(),
        clock=lambda: datetime(2026, 9, 22, 20, 18, tzinfo=timezone.utc),
    )

    with pytest.raises(ProphetXReadOnlyError, match="(?i)(origin|authority|canonical transport)"):
        client.read_account_snapshot(frozenset({BookmakerCapability.BALANCE_READ}))
