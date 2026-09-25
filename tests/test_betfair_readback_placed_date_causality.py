from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
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
    ProviderEvidenceError,
    VerifiedProviderEffectEvidence,
    verify_betfair_provider_state,
)


PROVIDER_REF = "c" * 32
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
        action_id="action-placed-date-causality",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-placed-date-causality",
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
        source_ref="betfair://profile/placed-date-causality-test",
        source_payload_sha256="a" * 64,
    )


def _capture(action: ExecutionAction, *, placed_date: str):
    current_order = {
        "betId": "bet-placed-date-causality",
        "marketId": action.market_id,
        "selectionId": int(action.selection_id),
        "side": action.side,
        "status": "EXECUTABLE",
        "placedDate": placed_date,
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


def _verify(action: ExecutionAction, *, placed_date: str):
    profile = _profile()
    capture = _capture(action, placed_date=placed_date)
    return verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )


def test_current_order_placed_after_quote_can_still_mint_effect_evidence() -> None:
    action = _action()

    evidence = _verify(
        action,
        placed_date="2026-09-21T18:00:01+00:00",
    )

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert evidence.accepted_stake == Decimal("1.0")


def test_current_order_placed_before_quote_cannot_mint_effect_evidence() -> None:
    action = _action()

    with pytest.raises(ProviderEvidenceError):
        _verify(
            action,
            placed_date="2026-09-21T17:59:59+00:00",
        )
