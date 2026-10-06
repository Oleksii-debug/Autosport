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


PROVIDER_REF = "e" * 32
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
        action_id="action-cleared-chronology",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-cleared-chronology",
        quote_observed_at="2026-09-21T17:59:00+00:00",
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
        source_ref="betfair://profile/cleared-chronology-test",
        source_payload_sha256="a" * 64,
    )


def _capture(action: ExecutionAction, *, settled_date: str):
    settled_order = {
        "betId": "bet-cleared-chronology",
        "marketId": action.market_id,
        "selectionId": int(action.selection_id),
        "side": action.side,
        "placedDate": "2026-09-21T18:00:01+00:00",
        "settledDate": settled_date,
        "priceRequested": 2.0,
        "priceMatched": 2.0,
        "sizeSettled": 10.0,
        "profit": 10.0,
        "customerOrderRef": PROVIDER_REF,
        "eventId": action.event_id,
    }
    responses = [
        _rpc_result(
            [{"marketId": action.market_id, "event": {"id": action.event_id}}],
            1,
        ),
        _rpc_result({"currentOrders": [], "moreAvailable": False}, 2),
    ]
    for request_id, status in enumerate(
        ("SETTLED", "VOIDED", "LAPSED", "CANCELLED"),
        start=3,
    ):
        responses.append(
            _rpc_result(
                {
                    "clearedOrders": [settled_order] if status == "SETTLED" else [],
                    "moreAvailable": False,
                },
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


def _verify(action: ExecutionAction, *, settled_date: str):
    profile = _profile()
    capture = _capture(action, settled_date=settled_date)
    return verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )


def test_cleared_order_settled_after_placement_can_mint_effect_evidence() -> None:
    action = _action()

    evidence = _verify(
        action,
        settled_date="2026-09-21T18:00:02+00:00",
    )

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert evidence.accepted_stake == Decimal("10.0")


def test_cleared_order_settled_before_placement_cannot_mint_effect_evidence() -> None:
    action = _action()

    # Both fields are individually valid ISO timestamps, but their causal order is
    # impossible for one cleared bet.  Provider-effect authority must fail closed
    # rather than treating contradictory chronology as harmless payload metadata.
    with pytest.raises(ProviderEvidenceError):
        _verify(
            action,
            settled_date="2026-09-21T18:00:00+00:00",
        )
