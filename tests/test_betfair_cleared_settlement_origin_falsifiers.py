from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

import autosport.betfair_cleared_settlement_evidence as settlement
from autosport.betfair_account_readonly import (
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


_FIXED_NOW = datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc)
_MARKET_ID = "1.234"
_EVENT_ID = "event-1"
_ORDER_REF = "abc123"


class _FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)

    def post(
        self,
        _url: str,
        *,
        headers: object,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        del headers, body, timeout_seconds
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _issued_readback() -> BetfairExecutionReadbackEnvelope:
    row = {
        "betId": "bet-1",
        "marketId": _MARKET_ID,
        "selectionId": 42,
        "side": "BACK",
        "placedDate": "2026-09-21T08:00:00+00:00",
        "settledDate": "2026-09-21T09:00:00+00:00",
        "priceRequested": 2.0,
        "priceMatched": 2.1,
        "sizeSettled": 10.0,
        "profit": 11.0,
        "customerOrderRef": _ORDER_REF,
        "eventId": _EVENT_ID,
    }
    responses = [
        _response(
            [{"marketId": _MARKET_ID, "event": {"id": _EVENT_ID}}],
            1,
        ),
        _response({"currentOrders": [], "moreAvailable": False}, 2),
        _response({"clearedOrders": [row], "moreAvailable": False}, 3),
        _response({"clearedOrders": [], "moreAvailable": False}, 4),
        _response({"clearedOrders": [], "moreAvailable": False}, 5),
        _response({"clearedOrders": [], "moreAvailable": False}, 6),
    ]
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=_FakeTransport(responses),
        clock=lambda: _FIXED_NOW,
    )
    return client.read_execution_readback(
        action_id="action-1",
        market_id=_MARKET_ID,
        provider_order_ref=_ORDER_REF,
    )


def test_readback_authority_class_rebind_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readback = _issued_readback()
    monkeypatch.setattr(
        BetfairExecutionReadbackEnvelope,
        "assert_authoritative",
        lambda _self: None,
    )

    with pytest.raises(
        settlement.BetfairClearedSettlementEvidenceError,
        match="canonical execution readback authority changed",
    ):
        settlement.resolve_betfair_cleared_bet_settlement(readback)


def test_private_resolver_retarget_cannot_mint_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readback = _issued_readback()
    baseline = settlement.resolve_betfair_cleared_bet_settlement(readback)
    assert baseline is not None
    forged = replace(baseline)

    monkeypatch.setattr(
        settlement,
        "_resolve_betfair_cleared_bet_settlement",
        lambda _readback: forged,
    )

    issued = settlement.resolve_betfair_cleared_bet_settlement(readback)
    assert issued is not None
    assert issued is not forged
    issued.assert_authoritative()
    with pytest.raises(
        settlement.BetfairClearedSettlementEvidenceError,
        match="not issued by canonical resolver",
    ):
        forged.assert_authoritative()


@pytest.mark.parametrize(
    "value",
    (
        Decimal("1e1000000"),
        Decimal("1e-1000000"),
        Decimal("-9e999999"),
    ),
)
def test_pathological_decimal_scale_fails_before_fixed_point_materialization(
    value: Decimal,
) -> None:
    with pytest.raises(
        settlement.BetfairClearedSettlementEvidenceError,
        match="decimal text is too large",
    ):
        settlement._decimal_text(value)


def test_zero_with_huge_exponent_is_canonical_without_expansion() -> None:
    assert settlement._decimal_text(Decimal("0e1000000")) == "0"
