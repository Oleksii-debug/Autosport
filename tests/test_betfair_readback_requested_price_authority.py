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


PROVIDER_REF = "a" * 32
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
        action_id="action-requested-price",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-requested-price",
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
        source_ref="betfair://profile/requested-price-test",
        source_payload_sha256="a" * 64,
    )


def _capture(
    action: ExecutionAction,
    *,
    surface: str,
    provider_requested_price: float | None,
    matched_price: float = 3.5,
):
    current_orders: list[dict[str, object]] = []
    cleared_by_status: dict[str, list[dict[str, object]]] = {
        "SETTLED": [],
        "VOIDED": [],
        "LAPSED": [],
        "CANCELLED": [],
    }

    if surface == "current":
        row: dict[str, object] = {
            "betId": "bet-current-requested-price",
            "marketId": action.market_id,
            "selectionId": int(action.selection_id),
            "side": action.side,
            "status": "EXECUTABLE",
            "placedDate": "2026-09-21T18:00:01+00:00",
            "averagePriceMatched": matched_price,
            "sizeMatched": 1.0,
            "sizeRemaining": 9.0,
            "customerOrderRef": PROVIDER_REF,
        }
        if provider_requested_price is not None:
            row["priceSize"] = {
                "price": provider_requested_price,
                "size": 10.0,
            }
        current_orders.append(row)
    elif surface in {"cleared", "cleared_conflict"}:
        assert provider_requested_price is not None
        cleared_row = {
            "betId": "bet-cleared-requested-price",
            "eventId": action.event_id,
            "marketId": action.market_id,
            "selectionId": int(action.selection_id),
            "side": action.side,
            "placedDate": "2026-09-21T18:00:01+00:00",
            "settledDate": "2026-09-21T18:00:02+00:00",
            "priceRequested": provider_requested_price,
            "priceMatched": matched_price,
            "sizeSettled": 10.0,
            "profit": 25.0,
            "customerOrderRef": PROVIDER_REF,
        }
        cleared_by_status["SETTLED"].append(dict(cleared_row))
        if surface == "cleared_conflict":
            cleared_by_status["VOIDED"].append(dict(cleared_row))
    else:  # pragma: no cover - tests below exhaust the supported surfaces.
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


def _verify(
    action: ExecutionAction,
    *,
    surface: str,
    requested_price: float | None,
    matched_price: float = 3.5,
):
    profile = _profile()
    capture = _capture(
        action,
        surface=surface,
        provider_requested_price=requested_price,
        matched_price=matched_price,
    )
    return _evaluate_betfair_provider_state_semantics(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )


def test_current_order_wrong_requested_price_cannot_mint_effect_evidence() -> None:
    action = _action()

    with pytest.raises(ProviderEvidenceError):
        _verify(action, surface="current", requested_price=3.5)


def test_current_order_missing_requested_price_cannot_mint_effect_evidence() -> None:
    action = _action()

    with pytest.raises(ProviderEvidenceError):
        _verify(action, surface="current", requested_price=None)


def test_cleared_order_wrong_requested_price_cannot_mint_effect_evidence() -> None:
    action = _action()

    with pytest.raises(ProviderEvidenceError):
        _verify(action, surface="cleared", requested_price=3.5)


def test_current_order_exact_requested_price_preserves_favorable_execution() -> None:
    action = _action()

    evidence = _verify(action, surface="current", requested_price=2.0)

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert evidence.accepted_odds == Decimal("3.5")
    assert evidence.accepted_stake == Decimal("1.0")


def test_cleared_order_exact_requested_price_preserves_favorable_execution() -> None:
    action = _action()

    evidence = _verify(action, surface="cleared", requested_price=2.0)

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert evidence.accepted_odds == Decimal("3.5")
    assert evidence.accepted_stake == Decimal("10.0")


@pytest.mark.parametrize("surface", ["current", "cleared"])
def test_back_match_cannot_be_worse_than_submitted_limit(surface: str) -> None:
    action = _action()

    with pytest.raises(
        ProviderEvidenceError,
        match="worse than submitted Betfair BACK limit",
    ):
        _verify(
            action,
            surface=surface,
            requested_price=2.0,
            matched_price=1.99,
        )


@pytest.mark.parametrize("surface", ["current", "cleared"])
def test_back_match_at_submitted_limit_remains_authoritative(surface: str) -> None:
    action = _action()

    evidence = _verify(
        action,
        surface=surface,
        requested_price=2.0,
        matched_price=2.0,
    )

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert evidence.accepted_odds == Decimal("2.0")


def test_same_receipt_in_multiple_cleared_statuses_fails_closed() -> None:
    action = _action()

    with pytest.raises(
        ProviderEvidenceError,
        match="contradictory cleared terminal statuses",
    ):
        _verify(
            action,
            surface="cleared_conflict",
            requested_price=2.0,
            matched_price=2.0,
        )
