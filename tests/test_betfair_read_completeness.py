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
    BetfairReadCompletenessWitness,
)


NOW = datetime(2026, 9, 21, 19, 30, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, outcomes: list[bytes | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
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
        if not self.outcomes:
            raise AssertionError("unexpected provider call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _rpc(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _rpc_error(message: str, request_id: int) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": message},
            "id": request_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _client(*outcomes: bytes | BaseException) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport(list(outcomes)),
        clock=lambda: NOW,
        venue_id="betfair",
        account_id="acct-1",
    )


def _observer(*outcomes: bytes | BaseException) -> BetfairReadCompletenessObserver:
    return BetfairReadCompletenessObserver(
        _client(*outcomes),
        clock=lambda: NOW,
    )


def _current_order(bet_id: str) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.234",
        "selectionId": 42,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T19:00:00+00:00",
        "averagePriceMatched": 0,
        "sizeMatched": 0,
        "sizeRemaining": 10,
    }


def _cleared_order(bet_id: str) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.234",
        "selectionId": 42,
        "side": "BACK",
        "placedDate": "2026-09-21T18:00:00+00:00",
        "settledDate": "2026-09-21T19:00:00+00:00",
        "priceRequested": 2,
        "priceMatched": 2,
        "sizeSettled": 10,
        "profit": 10,
        "eventId": "event-1",
    }


def test_successful_provider_end_empty_is_authoritative_empty() -> None:
    observer = _observer(_rpc({"currentOrders": [], "moreAvailable": False}, 1))

    result = observer.read_current_orders(page_size=10)

    assert result.items == ()
    assert result.witness.completeness is (
        BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
    )
    assert result.witness.authoritative is True
    assert result.authoritative_empty is True
    assert result.assert_complete() == ()
    assert result.witness.pages[0][2] is False


def test_throttled_read_is_explicit_transient_not_empty() -> None:
    observer = _observer(_rpc_error("TOO_MANY_REQUESTS", 1))

    result = observer.read_current_orders(page_size=10)

    assert result.items == ()
    assert result.witness.completeness is BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT
    assert result.witness.failure_code == "provider_transient"
    assert result.authoritative_empty is False
    with pytest.raises(BetfairReadOnlyError, match="not complete"):
        result.assert_complete()


def test_valid_first_page_then_timeout_is_partial_not_complete() -> None:
    observer = _observer(
        _rpc({"clearedOrders": [_cleared_order("bet-1")], "moreAvailable": True}, 1),
        _rpc_error("TIMEOUT_ERROR", 2),
    )

    result = observer.read_cleared_orders(page_size=1)

    assert [row.bet_id for row in result.items] == ["bet-1"]
    assert result.witness.completeness is BetfairObservationCompleteness.PARTIAL
    assert result.witness.failure_code == "acquisition_interrupted"
    assert result.witness.rows_observed == 1
    assert len(result.witness.pages) == 1
    assert result.witness.pages[0][2] is True
    with pytest.raises(BetfairReadOnlyError, match="not complete"):
        result.assert_complete()


def test_invalid_session_routes_to_auth_recovery_state() -> None:
    observer = _observer(_rpc_error("INVALID_SESSION_INFORMATION", 1))

    result = observer.read_account_funds()

    assert result.value is None
    assert result.witness.completeness is BetfairObservationCompleteness.AUTH_INVALID_OR_EXPIRED
    assert result.witness.failure_code == "provider_auth"
    with pytest.raises(BetfairReadOnlyError, match="not complete"):
        result.assert_complete()


def test_service_busy_account_funds_does_not_synthesize_zero_balance() -> None:
    observer = _observer(_rpc_error("SERVICE_BUSY", 1))

    result = observer.read_account_funds()

    assert result.value is None
    assert result.witness.completeness is BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT
    assert result.witness.rows_observed == 0


def test_mismatched_jsonrpc_id_is_contract_failure_not_provider_state() -> None:
    observer = _observer(_rpc({"currentOrders": [], "moreAvailable": False}, 99))

    result = observer.read_current_orders()

    assert result.items == ()
    assert result.witness.completeness is (
        BetfairObservationCompleteness.INVALID_REQUEST_OR_CONTRACT
    )
    assert result.authoritative_empty is False


def test_cross_page_overlap_is_deduped_but_completeness_fails_closed() -> None:
    observer = _observer(
        _rpc({"currentOrders": [_current_order("same")], "moreAvailable": True}, 1),
        _rpc({"currentOrders": [_current_order("same")], "moreAvailable": False}, 2),
    )

    result = observer.read_current_orders(page_size=1)

    assert [row.bet_id for row in result.items] == ["same"]
    assert result.witness.completeness is BetfairObservationCompleteness.PARTIAL
    assert result.witness.failure_code == "cross_page_duplicate"
    assert len(result.witness.pages) == 2


def test_pagination_budget_exhaustion_is_partial_even_with_valid_page() -> None:
    observer = _observer(
        _rpc({"currentOrders": [_current_order("bet-1")], "moreAvailable": True}, 1)
    )

    result = observer.read_current_orders(page_size=1, max_pages=1)

    assert [row.bet_id for row in result.items] == ["bet-1"]
    assert result.witness.completeness is BetfairObservationCompleteness.PARTIAL
    assert result.witness.failure_code == "pagination_limit"


def test_query_scope_changes_completeness_identity() -> None:
    left = _observer(_rpc({"currentOrders": [], "moreAvailable": False}, 1))
    right = _observer(_rpc({"currentOrders": [], "moreAvailable": False}, 1))

    left_result = left.read_current_orders(market_ids=("1.111",))
    right_result = right.read_current_orders(market_ids=("1.222",))

    assert left_result.witness.query_sha256 != right_result.witness.query_sha256
    assert left_result.witness.attempt_id != right_result.witness.attempt_id
    assert left_result.witness.authoritative
    assert right_result.witness.authoritative


def test_caller_constructed_complete_witness_is_not_authoritative() -> None:
    forged = BetfairReadCompletenessWitness(
        operation="listCurrentOrders",
        completeness=BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
        query_sha256="a" * 64,
        attempt_id="b" * 64,
        started_at=NOW.isoformat(),
        finished_at=NOW.isoformat(),
        pages=((0, 1000, False, "c" * 64),),
        rows_observed=0,
    )

    assert forged.authoritative is False
    with pytest.raises(BetfairReadOnlyError, match="not issued"):
        forged.assert_authoritative()


def test_successful_account_funds_is_complete_and_never_float_money() -> None:
    observer = _observer(
        _rpc(
            {
                "availableToBetBalance": 100,
                "exposure": -5,
                "retainedCommission": 0,
                "exposureLimit": -1000,
            },
            1,
        )
    )

    result = observer.read_account_funds()
    funds = result.assert_complete()

    assert str(funds.available_to_bet_balance) == "100"
    assert result.witness.completeness is (
        BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
    )
    assert result.witness.authoritative

def test_multi_page_current_orders_end_is_not_atomic_snapshot_completeness() -> None:
    observer = _observer(
        _rpc({"currentOrders": [_current_order("bet-1")], "moreAvailable": True}, 1),
        _rpc({"currentOrders": [_current_order("bet-2")], "moreAvailable": False}, 2),
    )

    result = observer.read_current_orders(page_size=1)

    assert [row.bet_id for row in result.items] == ["bet-1", "bet-2"]
    assert len(result.witness.pages) == 2
    assert result.witness.pages[-1][2] is False
    assert result.witness.completeness is BetfairObservationCompleteness.PARTIAL
    assert result.witness.failure_code == "cross_call_snapshot_unproven"
    assert result.witness.authoritative is False
    with pytest.raises(BetfairReadOnlyError, match="not complete"):
        result.assert_complete()


def test_multi_page_cleared_orders_end_is_not_atomic_snapshot_completeness() -> None:
    observer = _observer(
        _rpc({"clearedOrders": [_cleared_order("bet-1")], "moreAvailable": True}, 1),
        _rpc({"clearedOrders": [_cleared_order("bet-2")], "moreAvailable": False}, 2),
    )

    result = observer.read_cleared_orders(page_size=1)

    assert [row.bet_id for row in result.items] == ["bet-1", "bet-2"]
    assert len(result.witness.pages) == 2
    assert result.witness.pages[-1][2] is False
    assert result.witness.completeness is BetfairObservationCompleteness.PARTIAL
    assert result.witness.failure_code == "cross_call_snapshot_unproven"
    assert result.witness.authoritative is False
    with pytest.raises(BetfairReadOnlyError, match="not complete"):
        result.assert_complete()

