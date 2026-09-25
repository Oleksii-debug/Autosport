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
    _evaluate_betfair_provider_state_semantics,
)


PROVIDER_REF = "c" * 32
OBSERVED_AT = "2026-09-23T09:40:16+00:00"


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
        action_id="action-lay-price-bound",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="LAY",
        requested_odds=Decimal("3.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-lay-price-bound",
        quote_observed_at="2026-09-23T09:39:00+00:00",
        expires_at="2026-09-23T09:50:00+00:00",
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
        observed_at="2026-09-23T09:39:00+00:00",
        source_ref="betfair://profile/lay-price-bound-test",
        source_payload_sha256="a" * 64,
    )


def _capture(
    action: ExecutionAction,
    *,
    surface: str,
    matched_price: float,
):
    current_orders: list[dict[str, object]] = []
    cleared_by_status: dict[str, list[dict[str, object]]] = {
        "SETTLED": [],
        "VOIDED": [],
        "LAPSED": [],
        "CANCELLED": [],
    }

    if surface == "current":
        current_orders.append(
            {
                "betId": "bet-current-lay-price-bound",
                "marketId": action.market_id,
                "selectionId": int(action.selection_id),
                "side": action.side,
                "status": "EXECUTABLE",
                "placedDate": "2026-09-23T09:40:01+00:00",
                "priceSize": {"price": 3.0, "size": 10.0},
                "averagePriceMatched": matched_price,
                "sizeMatched": 1.0,
                "sizeRemaining": 9.0,
                "customerOrderRef": PROVIDER_REF,
            }
        )
    elif surface == "cleared":
        cleared_by_status["SETTLED"].append(
            {
                "betId": "bet-cleared-lay-price-bound",
                "eventId": action.event_id,
                "marketId": action.market_id,
                "selectionId": int(action.selection_id),
                "side": action.side,
                "placedDate": "2026-09-23T09:40:01+00:00",
                "settledDate": "2026-09-23T09:40:02+00:00",
                "priceRequested": 3.0,
                "priceMatched": matched_price,
                "sizeSettled": 10.0,
                "profit": 10.0,
                "customerOrderRef": PROVIDER_REF,
            }
        )
    else:  # pragma: no cover - parametrized tests exhaust supported surfaces.
        raise AssertionError(f"unsupported surface {surface}")

    responses = [
        _rpc_result(
            [{"marketId": action.market_id, "event": {"id": action.event_id}}],
            1,
        ),
        _rpc_result(
            {"currentOrders": current_orders, "moreAvailable": False},
            2,
        ),
    ]
    for request_id, status in enumerate(
        ("SETTLED", "VOIDED", "LAPSED", "CANCELLED"),
        start=3,
    ):
        responses.append(
            _rpc_result(
                {
                    "clearedOrders": cleared_by_status[status],
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


def _verify(*, surface: str, matched_price: float):
    action = _action()
    profile = _profile()
    capture = _capture(action, surface=surface, matched_price=matched_price)
    return _evaluate_betfair_provider_state_semantics(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )


@pytest.mark.parametrize("surface", ["current", "cleared"])
def test_lay_match_above_submitted_limit_cannot_mint_effect(surface: str) -> None:
    with pytest.raises(
        ProviderEvidenceError,
        match="worse than submitted Betfair LAY limit",
    ):
        _verify(surface=surface, matched_price=3.01)


@pytest.mark.parametrize("surface", ["current", "cleared"])
@pytest.mark.parametrize("matched_price", [3.0, 2.5])
def test_lay_match_at_or_better_than_limit_remains_authoritative(
    surface: str,
    matched_price: float,
) -> None:
    evidence = _verify(surface=surface, matched_price=matched_price)

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert evidence.accepted_odds == Decimal(str(matched_price))
