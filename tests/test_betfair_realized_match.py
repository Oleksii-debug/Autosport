from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_realized_match import (
    RealizedMatchEvidenceError,
    RealizedMatchSource,
    resolve_betfair_realized_match,
)
from autosport.real_execution_ledger import ExecutionAction


FIXED_NOW = datetime(2026, 9, 21, 9, 55, tzinfo=timezone.utc)
PROVIDER_ORDER_REF = "a" * 32
MARKET_ID = "1.234"
EVENT_ID = "event-1"
SELECTION_ID = 10


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": result,
            "id": request_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _action(
    *,
    requested_odds: str = "3.0",
    requested_stake: str = "10",
) -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id=EVENT_ID,
        market_id=MARKET_ID,
        selection_id=str(SELECTION_ID),
        side="BACK",
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        quote_id="quote-1",
        quote_observed_at="2026-09-21T09:54:50+00:00",
        expires_at="2026-09-21T09:56:00+00:00",
    )


def _current_order(
    *,
    bet_id: str = "bet-1",
    customer_order_ref: str = PROVIDER_ORDER_REF,
    price: float = 3.0,
    requested_size: float = 10.0,
    average_price_matched: float = 3.2,
    size_matched: float = 4.0,
    size_remaining: float = 6.0,
) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T09:54:55+00:00",
        "priceSize": {
            "price": price,
            "size": requested_size,
        },
        "averagePriceMatched": average_price_matched,
        "sizeMatched": size_matched,
        "sizeRemaining": size_remaining,
        "customerOrderRef": customer_order_ref,
    }


def _cleared_order(
    *,
    bet_id: str = "bet-1",
    customer_order_ref: str = PROVIDER_ORDER_REF,
    price_requested: float = 3.0,
    price_matched: float = 3.2,
    size_settled: float = 10.0,
    profit: float = 22.0,
) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "placedDate": "2026-09-21T09:54:55+00:00",
        "settledDate": "2026-09-21T10:30:00+00:00",
        "priceRequested": price_requested,
        "priceMatched": price_matched,
        "sizeSettled": size_settled,
        "profit": profit,
        "customerOrderRef": customer_order_ref,
        "eventId": EVENT_ID,
    }


def _capture(
    *,
    current_orders: list[dict[str, object]] | None = None,
    cleared_by_status: dict[str, list[dict[str, object]]] | None = None,
):
    current_orders = current_orders or []
    cleared_by_status = cleared_by_status or {}
    statuses = ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
    responses = [
        _response(
            [{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}],
            1,
        ),
        _response(
            {
                "currentOrders": current_orders,
                "moreAvailable": False,
            },
            2,
        ),
    ]
    for request_id, status in enumerate(statuses, 3):
        responses.append(
            _response(
                {
                    "clearedOrders": cleared_by_status.get(status, []),
                    "moreAvailable": False,
                },
                request_id,
            )
        )

    transport = FakeTransport(responses)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair",
        account_id="acct-1",
    )
    capture = client.read_execution_readback(
        action_id="action-1",
        provider_order_ref=PROVIDER_ORDER_REF,
        market_id=MARKET_ID,
    )
    assert len(transport.calls) == 6
    return capture


def test_cleared_bet_truth_overrides_transient_current_match() -> None:
    action = _action()
    capture = _capture(
        current_orders=[
            _current_order(
                average_price_matched=3.1,
                size_matched=4.0,
                size_remaining=6.0,
            )
        ],
        cleared_by_status={
            "SETTLED": [
                _cleared_order(
                    price_matched=3.2,
                    size_settled=10.0,
                )
            ]
        },
    )

    evidence = resolve_betfair_realized_match(
        action,
        capture,
        expected_provider_order_ref=PROVIDER_ORDER_REF,
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.CLEARED_BET
    assert evidence.finalized is True
    assert evidence.requested_odds == Decimal("3.0")
    assert evidence.provider_matched_odds == Decimal("3.2")
    assert evidence.provider_matched_stake == Decimal("10.0")
    assert evidence.unrealized_requested_stake == Decimal("0")
    assert evidence.bet_id == "bet-1"
    assert evidence.provider_status == "SETTLED"
    assert evidence.has_matched_economics is True


def test_partial_current_order_is_transient_realized_match_evidence() -> None:
    action = _action()
    capture = _capture(
        current_orders=[
            _current_order(
                average_price_matched=3.2,
                size_matched=4.0,
                size_remaining=6.0,
            )
        ]
    )

    evidence = resolve_betfair_realized_match(
        action,
        capture,
        expected_provider_order_ref=PROVIDER_ORDER_REF,
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.CURRENT_ORDER
    assert evidence.finalized is False
    assert evidence.provider_matched_odds == Decimal("3.2")
    assert evidence.provider_matched_stake == Decimal("4.0")
    assert evidence.unrealized_requested_stake == Decimal("6.0")
    assert evidence.has_matched_economics is True


def test_no_provider_order_row_is_incomplete_not_synthetic_zero() -> None:
    evidence = resolve_betfair_realized_match(
        _action(),
        _capture(),
        expected_provider_order_ref=PROVIDER_ORDER_REF,
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.INCOMPLETE_EVIDENCE
    assert evidence.finalized is False
    assert evidence.bet_id is None
    assert evidence.provider_matched_odds is None
    assert evidence.provider_matched_stake is None
    assert evidence.unrealized_requested_stake is None
    assert evidence.has_matched_economics is False


def test_cleared_zero_match_is_authoritative_zero_realized_stake() -> None:
    evidence = resolve_betfair_realized_match(
        _action(),
        _capture(
            cleared_by_status={
                "LAPSED": [
                    _cleared_order(
                        price_matched=0.0,
                        size_settled=0.0,
                        profit=0.0,
                    )
                ]
            }
        ),
        expected_provider_order_ref=PROVIDER_ORDER_REF,
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.CLEARED_BET
    assert evidence.finalized is True
    assert evidence.provider_status == "LAPSED"
    assert evidence.provider_matched_odds is None
    assert evidence.provider_matched_stake == Decimal("0")
    assert evidence.unrealized_requested_stake == Decimal("10")
    assert evidence.has_matched_economics is False


def test_cleared_price_requested_must_match_execution_action() -> None:
    capture = _capture(
        cleared_by_status={
            "SETTLED": [
                _cleared_order(
                    price_requested=2.8,
                    price_matched=3.2,
                )
            ]
        }
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="requested price differs",
    ):
        resolve_betfair_realized_match(
            _action(),
            capture,
            expected_provider_order_ref=PROVIDER_ORDER_REF,
        )


def test_returned_row_must_keep_exact_customer_order_identity() -> None:
    capture = _capture(
        current_orders=[
            _current_order(
                customer_order_ref="b" * 32,
            )
        ]
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="exact durable customer order reference",
    ):
        resolve_betfair_realized_match(
            _action(),
            capture,
            expected_provider_order_ref=PROVIDER_ORDER_REF,
        )


def test_multiple_provider_bet_ids_for_one_order_reference_fail_closed() -> None:
    capture = _capture(
        current_orders=[_current_order(bet_id="bet-current")],
        cleared_by_status={
            "SETTLED": [
                _cleared_order(
                    bet_id="bet-cleared",
                    price_matched=3.2,
                    size_settled=10.0,
                )
            ]
        },
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="multiple bet ids",
    ):
        resolve_betfair_realized_match(
            _action(),
            capture,
            expected_provider_order_ref=PROVIDER_ORDER_REF,
        )


def test_forged_readback_copy_cannot_mint_realized_match_evidence() -> None:
    capture = _capture(
        current_orders=[_current_order()]
    )
    forged = replace(capture)

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="not canonical provider evidence",
    ):
        resolve_betfair_realized_match(
            _action(),
            forged,
            expected_provider_order_ref=PROVIDER_ORDER_REF,
        )


def test_copied_realized_match_result_is_not_authoritative() -> None:
    evidence = resolve_betfair_realized_match(
        _action(),
        _capture(current_orders=[_current_order()]),
        expected_provider_order_ref=PROVIDER_ORDER_REF,
    )
    copied = replace(evidence)

    evidence.assert_authoritative()
    with pytest.raises(
        RealizedMatchEvidenceError,
        match="not issued by canonical resolver",
    ):
        copied.assert_authoritative()


def test_provider_revision_changes_evidence_without_rewriting_decision_quote() -> None:
    action = _action()
    transient = resolve_betfair_realized_match(
        action,
        _capture(
            current_orders=[
                _current_order(
                    average_price_matched=3.1,
                    size_matched=4.0,
                    size_remaining=6.0,
                )
            ]
        ),
        expected_provider_order_ref=PROVIDER_ORDER_REF,
    )
    final = resolve_betfair_realized_match(
        action,
        _capture(
            cleared_by_status={
                "SETTLED": [
                    _cleared_order(
                        price_matched=3.2,
                        size_settled=10.0,
                    )
                ]
            }
        ),
        expected_provider_order_ref=PROVIDER_ORDER_REF,
    )

    transient.assert_authoritative()
    final.assert_authoritative()
    assert transient.requested_odds == final.requested_odds == Decimal("3.0")
    assert transient.provider_matched_odds == Decimal("3.1")
    assert final.provider_matched_odds == Decimal("3.2")
    assert transient.evidence_id != final.evidence_id
    assert transient.finalized is False
    assert final.finalized is True


def test_current_order_matched_plus_remaining_cannot_exceed_request() -> None:
    capture = _capture(
        current_orders=[
            _current_order(
                size_matched=7.0,
                size_remaining=4.0,
            )
        ]
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="matched plus remaining",
    ):
        resolve_betfair_realized_match(
            _action(),
            capture,
            expected_provider_order_ref=PROVIDER_ORDER_REF,
        )


def test_expected_provider_order_reference_must_match_capture_scope() -> None:
    with pytest.raises(
        RealizedMatchEvidenceError,
        match="provider order reference mismatch",
    ):
        resolve_betfair_realized_match(
            _action(),
            _capture(current_orders=[_current_order()]),
            expected_provider_order_ref="c" * 32,
        )
