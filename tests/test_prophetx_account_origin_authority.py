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


_CALLER_AUTHORED_BODY = (
    b'{"data":{"balance":999999.00,"gec_balance":0,'
    b'"matched_order_balance":0,"unmatched_order_balance":0,'
    b'"unmatched_order_balance_status":"succeed",'
    b'"unmatched_order_last_synced_at":"2026-09-22T19:00:00Z"}}'
)


class CallerInjectedTransport:
    """Structurally valid transport with no product-owned network-origin authority."""

    def get(self, url, *, headers, timeout_seconds):
        del url, headers, timeout_seconds
        return ProphetXHttpResponse(
            status=200,
            final_url=BALANCE_URL,
            content_type="application/json",
            content_encoding=None,
            body=_CALLER_AUTHORED_BODY,
        )


def _client() -> ProphetXReadOnlyClient:
    return ProphetXReadOnlyClient(
        ProphetXSessionToken("caller-known-test-token"),
        transport=CallerInjectedTransport(),
        clock=lambda: datetime(2026, 9, 22, 19, 1, tzinfo=timezone.utc),
        venue_id="prophetx",
        account_id="account-a",
    )


def test_injected_transport_cannot_mint_supported_balance_capability() -> None:
    """A caller-authored response cannot become positive provider capability truth."""

    with pytest.raises(ProphetXReadOnlyError, match="origin"):
        _client().capability_profile()


def test_injected_transport_cannot_mint_canonical_account_balance_snapshot() -> None:
    """Canonical available cash needs product-owned provider-origin acquisition."""

    with pytest.raises(ProphetXReadOnlyError, match="origin"):
        _client().read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )
