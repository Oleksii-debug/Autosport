from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_read_completeness import (
    BetfairObservationCompleteness,
    BetfairReadCompletenessObserver,
)


class OneResponseTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.used = False

    def post(self, url, *, headers, body, timeout_seconds):
        if self.used:
            raise AssertionError("unexpected second provider call")
        self.used = True
        return self.payload


def test_official_api_ng_nested_error_code_can_never_launder_into_empty() -> None:
    """Opaque JSON-RPC message + semantic error.data remains non-authoritative.

    Betfair API-NG error examples may carry an opaque ANGX message while the semantic
    APING errorCode (for example INVALID_APP_KEY or TOO_MANY_REQUESTS) lives under
    ``error.data``.  The canonical client does not preserve that nested code yet.
    Until it does, this regression requires the completeness layer to fail closed:
    unknown classification is acceptable, authoritative empty is not.
    """

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "error": {
                "code": -32099,
                "message": "ANGX-0007",
                "data": {
                    "exceptionname": "APINGException",
                    "APINGException": {
                        "errorCode": "TOO_MANY_REQUESTS",
                        "errorDetails": "provider-controlled-details",
                    },
                },
            },
            "id": 1,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=OneResponseTransport(payload),
        clock=lambda: datetime(2026, 9, 21, 19, 45, tzinfo=timezone.utc),
    )
    observer = BetfairReadCompletenessObserver(
        client,
        clock=lambda: datetime(2026, 9, 21, 19, 45, tzinfo=timezone.utc),
    )

    result = observer.read_current_orders()

    assert result.items == ()
    assert result.authoritative_empty is False
    assert result.witness.completeness is not (
        BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
    )
    with pytest.raises(BetfairReadOnlyError, match="not complete"):
        result.assert_complete()
