from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_cleared_settlement_evidence import (
    BetfairClearedBetSettlementEvidence,
    BetfairClearedSettlementEvidenceError,
    resolve_betfair_cleared_bet_settlement,
)


FIXED_NOW = datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc)
ORDER_REF = "abc123"
MARKET_ID = "1.234"
EVENT_ID = "event-1"
BET_ID = "bet-1"


class FakeTransport:
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


def _row(
    *,
    bet_id: str = BET_ID,
    status: str = "SETTLED",
    selection_id: int = 42,
    side: str = "BACK",
    market_id: str = MARKET_ID,
    event_id: str | None = EVENT_ID,
    order_ref: str | None = ORDER_REF,
    price_requested: float = 2.0,
    price_matched: float = 2.1,
    size_settled: float = 10.0,
    profit: float = 11.0,
) -> dict[str, object]:
    del status
    row: dict[str, object] = {
        "betId": bet_id,
        "marketId": market_id,
        "selectionId": selection_id,
        "side": side,
        "placedDate": "2026-09-21T08:00:00+00:00",
        "settledDate": "2026-09-21T09:00:00+00:00",
        "priceRequested": price_requested,
        "priceMatched": price_matched,
        "sizeSettled": size_settled,
        "profit": profit,
    }
    if order_ref is not None:
        row["customerOrderRef"] = order_ref
    if event_id is not None:
        row["eventId"] = event_id
    return row


def _issued_readback(
    *,
    provider_order_ref: str | None = ORDER_REF,
    settled: list[dict[str, object]] | None = None,
    voided: list[dict[str, object]] | None = None,
    lapsed: list[dict[str, object]] | None = None,
    cancelled: list[dict[str, object]] | None = None,
) -> BetfairExecutionReadbackEnvelope:
    responses = [
        _response(
            [{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}],
            1,
        ),
        _response({"currentOrders": [], "moreAvailable": False}, 2),
        _response(
            {
                "clearedOrders": settled or [],
                "moreAvailable": False,
            },
            3,
        ),
        _response(
            {
                "clearedOrders": voided or [],
                "moreAvailable": False,
            },
            4,
        ),
        _response(
            {
                "clearedOrders": lapsed or [],
                "moreAvailable": False,
            },
            5,
        ),
        _response(
            {
                "clearedOrders": cancelled or [],
                "moreAvailable": False,
            },
            6,
        ),
    ]
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport(responses),
        clock=lambda: FIXED_NOW,
    )
    return client.read_execution_readback(
        action_id="action-1",
        market_id=MARKET_ID,
        provider_order_ref=provider_order_ref,
    )


def test_preserves_provider_signed_profit_and_origin_authority() -> None:
    readback = _issued_readback(
        settled=[_row(profit=-10.0)],
    )

    evidence = resolve_betfair_cleared_bet_settlement(readback)

    assert evidence is not None
    evidence.assert_authoritative()
    assert evidence.bet_id == BET_ID
    assert evidence.market_id == MARKET_ID
    assert evidence.event_id == EVENT_ID
    assert evidence.selection_id == 42
    assert evidence.side == "BACK"
    assert evidence.provider_order_ref == ORDER_REF
    assert evidence.settled_gross_profit == Decimal("-10.0")
    assert evidence.account_currency_bound is False


def test_preserves_multiple_terminal_status_rows_for_same_bet() -> None:
    readback = _issued_readback(
        settled=[
            _row(
                size_settled=6.0,
                price_matched=2.1,
                profit=6.6,
            )
        ],
        cancelled=[
            _row(
                size_settled=0.0,
                price_matched=0.0,
                profit=0.0,
            )
        ],
    )

    evidence = resolve_betfair_cleared_bet_settlement(readback)

    assert evidence is not None
    assert tuple(row.bet_status for row in evidence.terminal_rows) == (
        "SETTLED",
        "CANCELLED",
    )
    assert evidence.settled_gross_profit == Decimal("6.6")


def test_empty_terminal_readback_produces_no_positive_evidence() -> None:
    readback = _issued_readback()

    assert resolve_betfair_cleared_bet_settlement(readback) is None


def test_durable_provider_order_ref_is_required() -> None:
    readback = _issued_readback(
        provider_order_ref=None,
        settled=[_row(order_ref="action-1")],
    )

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="requires durable provider_order_ref",
    ):
        resolve_betfair_cleared_bet_settlement(readback)


def test_multiple_distinct_provider_bet_ids_fail_closed() -> None:
    readback = _issued_readback(
        settled=[_row(bet_id="bet-1")],
        cancelled=[
            _row(
                bet_id="bet-2",
                price_matched=0.0,
                size_settled=0.0,
                profit=0.0,
            )
        ],
    )

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="multiple bet ids",
    ):
        resolve_betfair_cleared_bet_settlement(readback)


def test_customer_order_ref_mismatch_fails_closed() -> None:
    readback = _issued_readback(
        settled=[_row(order_ref="def456")],
    )

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="exact durable provider order reference",
    ):
        resolve_betfair_cleared_bet_settlement(readback)


def test_event_identity_mismatch_fails_closed() -> None:
    readback = _issued_readback(
        settled=[_row(event_id="event-2")],
    )

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="event identity conflicts",
    ):
        resolve_betfair_cleared_bet_settlement(readback)


def test_selection_or_side_drift_across_terminal_rows_fails_closed() -> None:
    readback = _issued_readback(
        settled=[_row(selection_id=42, side="BACK")],
        cancelled=[
            _row(
                selection_id=43,
                side="BACK",
                price_matched=0.0,
                size_settled=0.0,
                profit=0.0,
            )
        ],
    )

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="disagree on selection or side",
    ):
        resolve_betfair_cleared_bet_settlement(readback)


def test_copied_positive_evidence_loses_origin_authority() -> None:
    readback = _issued_readback(settled=[_row()])
    evidence = resolve_betfair_cleared_bet_settlement(readback)
    assert evidence is not None

    copied = replace(evidence)

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="not issued by canonical resolver",
    ):
        copied.assert_authoritative()


def test_post_issuance_economic_tampering_is_detected() -> None:
    readback = _issued_readback(settled=[_row(profit=5.0)])
    evidence = resolve_betfair_cleared_bet_settlement(readback)
    assert evidence is not None

    forged_row = replace(evidence.terminal_rows[0], profit=Decimal("999"))
    object.__setattr__(evidence, "terminal_rows", (forged_row,))

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="digest mismatch",
    ):
        evidence.assert_authoritative()


def test_malformed_terminal_status_tampering_fails_with_domain_error() -> None:
    readback = _issued_readback(settled=[_row()])
    evidence = resolve_betfair_cleared_bet_settlement(readback)
    assert evidence is not None

    forged_row = replace(evidence.terminal_rows[0], bet_status="UNKNOWN_STATUS")
    object.__setattr__(evidence, "terminal_rows", (forged_row,))

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="unsupported terminal status",
    ):
        evidence.assert_authoritative()


def test_caller_constructed_evidence_cannot_mint_provider_authority() -> None:
    readback = _issued_readback(settled=[_row()])
    issued = resolve_betfair_cleared_bet_settlement(readback)
    assert issued is not None

    forged = BetfairClearedBetSettlementEvidence(
        venue_id=issued.venue_id,
        account_id=issued.account_id,
        adapter_id=issued.adapter_id,
        adapter_version=issued.adapter_version,
        action_id=issued.action_id,
        provider_order_ref=issued.provider_order_ref,
        event_id=issued.event_id,
        market_id=issued.market_id,
        bet_id=issued.bet_id,
        selection_id=issued.selection_id,
        side=issued.side,
        terminal_rows=issued.terminal_rows,
        readback_observed_at=issued.readback_observed_at,
        request_scope_sha256=issued.request_scope_sha256,
        readback_evidence_sha256=issued.readback_evidence_sha256,
        evidence_id=issued.evidence_id,
    )

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="not issued by canonical resolver",
    ):
        forged.assert_authoritative()


def test_evidence_identity_is_decimal_context_independent() -> None:
    with localcontext() as context:
        context.prec = 6
        low = resolve_betfair_cleared_bet_settlement(
            _issued_readback(settled=[_row(profit=-12.3456789)])
        )
    with localcontext() as context:
        context.prec = 50
        high = resolve_betfair_cleared_bet_settlement(
            _issued_readback(settled=[_row(profit=-12.3456789)])
        )

    assert low is not None and high is not None
    assert low.evidence_id == high.evidence_id


def test_same_status_duplicate_across_provider_pages_fails_closed() -> None:
    first = _response(
        [{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}],
        1,
    )
    current = _response({"currentOrders": [], "moreAvailable": False}, 2)
    settled_page_1 = _response(
        {
            "clearedOrders": [_row()],
            "moreAvailable": True,
        },
        3,
    )
    settled_page_2 = _response(
        {
            "clearedOrders": [_row()],
            "moreAvailable": False,
        },
        4,
    )
    voided = _response({"clearedOrders": [], "moreAvailable": False}, 5)
    lapsed = _response({"clearedOrders": [], "moreAvailable": False}, 6)
    cancelled = _response({"clearedOrders": [], "moreAvailable": False}, 7)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport(
            [
                first,
                current,
                settled_page_1,
                settled_page_2,
                voided,
                lapsed,
                cancelled,
            ]
        ),
        clock=lambda: FIXED_NOW,
    )
    readback = client.read_execution_readback(
        action_id="action-1",
        market_id=MARKET_ID,
        provider_order_ref=ORDER_REF,
    )

    with pytest.raises(
        BetfairClearedSettlementEvidenceError,
        match="duplicate cleared row",
    ):
        resolve_betfair_cleared_bet_settlement(readback)
