from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.real_execution_ledger import ExecutionAction
from autosport.supervised_provider_evidence import (
    VerifiedProviderEffectEvidence,
    verify_betfair_provider_state,
)


PROVIDER_REF = "d" * 32
OBSERVED_AT = "2026-09-21T18:00:16+00:00"


class _ReadbackTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        del url, headers, body, timeout_seconds
        if not self.responses:
            raise AssertionError("unexpected Betfair readback call")
        return self.responses.pop(0)


def _rpc_result(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-current-status-enum",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-current-status-enum",
        quote_observed_at="2026-09-21T18:00:00+00:00",
        expires_at="2026-09-21T18:10:00+00:00",
    )


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at="2026-09-21T17:59:00+00:00",
        source_ref="betfair://profile/current-status-enum-test",
        source_payload_sha256="a" * 64,
    )


def _capture(action: ExecutionAction, *, status: str):
    current_order = {
        "betId": "bet-current-status-enum",
        "marketId": action.market_id,
        "selectionId": int(action.selection_id),
        "side": action.side,
        "status": status,
        "placedDate": "2026-09-21T18:00:01+00:00",
        "priceSize": {
            "price": 2.0,
            "size": 10.0,
        },
        "averagePriceMatched": 2.0,
        "sizeMatched": 1.0,
        "sizeRemaining": 9.0,
        "customerOrderRef": PROVIDER_REF,
    }
    responses = [
        _rpc_result(
            [{"marketId": action.market_id, "event": {"id": action.event_id}}],
            1,
        ),
        _rpc_result(
            {"currentOrders": [current_order], "moreAvailable": False},
            2,
        ),
    ]
    for request_id in range(3, 7):
        responses.append(
            _rpc_result(
                {"clearedOrders": [], "moreAvailable": False},
                request_id,
            )
        )

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=_ReadbackTransport(responses),
        clock=lambda: datetime.fromisoformat(OBSERVED_AT),
        venue_id="betfair",
        account_id="acct-1",
    )
    return client.read_execution_readback(
        action_id=action.action_id,
        market_id=action.market_id,
        provider_order_ref=PROVIDER_REF,
    )


def _verify(action: ExecutionAction, *, status: str):
    profile = _profile()
    capture = _capture(action, status=status)
    return verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )


def test_recognized_executable_current_status_can_still_mint_effect_evidence() -> None:
    action = _action()

    evidence = _verify(action, status="EXECUTABLE")

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert evidence.accepted_stake == Decimal("1.0")


def test_unknown_current_status_is_rejected_at_canonical_readback_boundary() -> None:
    action = _action()

    # The canonical adapter accepts only provider-defined CurrentOrderSummary states.
    # Unknown non-empty text must fail before an authoritative readback object can be
    # created, so it can never flow into VerifiedProviderEffectEvidence issuance.
    with pytest.raises(BetfairReadOnlyError, match="status must be one of"):
        _capture(action, status="PROVIDER_UNKNOWN_STATUS")
