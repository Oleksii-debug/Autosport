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


PROVIDER_REF = "b" * 32
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
        action_id="action-current-size-upperbound",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-current-size-upperbound",
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
        source_ref="betfair://profile/current-size-upperbound-test",
        source_payload_sha256="a" * 64,
    )


def _capture(
    action: ExecutionAction,
    *,
    requested_size: float,
    matched_size: float,
    remaining_size: float,
):
    current_order = {
        "betId": "bet-current-size-upperbound",
        "marketId": action.market_id,
        "selectionId": int(action.selection_id),
        "side": action.side,
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T18:00:01+00:00",
        "priceSize": {
            "price": 2.0,
            "size": requested_size,
        },
        "averagePriceMatched": 2.0,
        "sizeMatched": matched_size,
        "sizeRemaining": remaining_size,
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


def _verify(action: ExecutionAction, *, matched_size: float, remaining_size: float):
    profile = _profile()
    capture = _capture(
        action,
        requested_size=10.0,
        matched_size=matched_size,
        remaining_size=remaining_size,
    )
    return verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )


def test_coherent_current_size_components_can_still_mint_effect_evidence() -> None:
    action = _action()

    evidence = _verify(action, matched_size=1.0, remaining_size=9.0)

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert evidence.accepted_stake == Decimal("1.0")


def test_current_size_components_cannot_exceed_provider_requested_size() -> None:
    action = _action()

    # The provider-native requested size is exactly the durable 10-unit intent, so
    # this is independent of the requested-size binding falsifier in #1814.  A
    # current row claiming 1 matched + 10 remaining units would account for 11
    # live/filled units from an original size of 10.  Omitted cancelled/lapsed/
    # voided components can only make matched+remaining smaller than the original;
    # they cannot make this over-allocation coherent.
    with pytest.raises(ProviderEvidenceError):
        _verify(action, matched_size=1.0, remaining_size=10.0)
