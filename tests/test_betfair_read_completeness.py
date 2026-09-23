from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_read_completeness import (
    BetfairObservationCompleteness,
    BetfairPagedReadResult,
    BetfairReadCompletenessObserver,
    BetfairReadCompletenessWitness,
    BetfairValueReadResult,
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


def _client(
    *outcomes: bytes | BaseException,
    account_id: str = "acct-1",
) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport(list(outcomes)),
        clock=lambda: NOW,
        venue_id="betfair",
        account_id=account_id,
    )



class FabricatingBetfairReadOnlyClient(BetfairReadOnlyClient):
    def read_account_funds(self):  # type: ignore[override]
        raise AssertionError("subclass override must never become completeness authority")


def _observer(
    *outcomes: bytes | BaseException,
    account_id: str = "acct-1",
) -> BetfairReadCompletenessObserver:
    return BetfairReadCompletenessObserver(
        _client(*outcomes, account_id=account_id),
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



def test_completeness_observer_rejects_subclassed_client_origin() -> None:
    client = FabricatingBetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport([]),
        clock=lambda: NOW,
        venue_id="betfair",
        account_id="acct-1",
    )

    with pytest.raises(TypeError, match="exact BetfairReadOnlyClient"):
        BetfairReadCompletenessObserver(client)


def test_successful_provider_end_empty_is_authoritative_empty() -> None:
    observer = _observer(_rpc({"currentOrders": [], "moreAvailable": False}, 1))

    result = observer.read_current_orders(page_size=10)

    assert result.items == ()
    assert result.witness.completeness is (
        BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
    )
    result.witness.assert_issued()
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
    result.witness.assert_issued()
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
    assert result.witness.failure_code == "partial_provider_transient"
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




def test_complete_witness_rejects_finished_before_started() -> None:
    with pytest.raises(BetfairReadOnlyError, match="finished_at"):
        BetfairReadCompletenessWitness(
            operation="listCurrentOrders",
            completeness=BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
            venue_id="betfair",
            account_id="acct-1",
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            query_sha256="a" * 64,
            attempt_id="b" * 64,
            started_at=NOW.isoformat(),
            finished_at=(NOW - timedelta(microseconds=1)).isoformat(),
            pages=((0, 1000, False, "c" * 64),),
            rows_observed=0,
        )


def test_observer_clock_regression_cannot_issue_authoritative_complete_witness() -> None:
    clock_values = iter((NOW, NOW - timedelta(seconds=1)))
    observer = BetfairReadCompletenessObserver(
        _client(_rpc({"currentOrders": [], "moreAvailable": False}, 1)),
        clock=lambda: next(clock_values),
    )

    with pytest.raises(BetfairReadOnlyError, match="finished_at"):
        observer.read_current_orders(page_size=10)


def test_zero_duration_complete_interval_remains_structurally_valid() -> None:
    witness = BetfairReadCompletenessWitness(
        operation="listCurrentOrders",
        completeness=BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        query_sha256="a" * 64,
        attempt_id="b" * 64,
        started_at=NOW.isoformat(),
        finished_at=NOW.isoformat(),
        pages=((0, 1000, False, "c" * 64),),
        rows_observed=0,
    )

    assert witness.started_at == witness.finished_at
    assert witness.authoritative is False

def test_identical_empty_reads_are_bound_to_distinct_configured_accounts() -> None:
    payload = _rpc({"currentOrders": [], "moreAvailable": False}, 1)
    left = _observer(payload, account_id="acct-a").read_current_orders(page_size=10)
    right = _observer(payload, account_id="acct-b").read_current_orders(page_size=10)

    assert left.authoritative_empty is True
    assert right.authoritative_empty is True
    assert left.witness.venue_id == right.witness.venue_id == "betfair"
    assert left.witness.account_id == "acct-a"
    assert right.witness.account_id == "acct-b"
    assert left.witness.adapter_id == right.witness.adapter_id
    assert left.witness.adapter_version == right.witness.adapter_version
    assert left.witness.query_sha256 != right.witness.query_sha256
    assert left.witness.attempt_id != right.witness.attempt_id
    left.witness.assert_authoritative_for(venue_id="betfair", account_id="acct-a")
    with pytest.raises(BetfairReadOnlyError, match="account scope mismatch"):
        left.witness.assert_authoritative_for(
            venue_id="betfair",
            account_id="acct-b",
        )



def test_caller_constructed_incomplete_witness_is_not_product_issued() -> None:
    forged = BetfairReadCompletenessWitness(
        operation="listCurrentOrders",
        completeness=BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT,
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        query_sha256="a" * 64,
        attempt_id="b" * 64,
        started_at=NOW.isoformat(),
        finished_at=NOW.isoformat(),
        pages=(),
        rows_observed=0,
        failure_code="provider_transient",
    )

    with pytest.raises(BetfairReadOnlyError, match="not issued"):
        forged.assert_issued()
    assert forged.authoritative is False

def test_caller_constructed_complete_witness_is_not_authoritative() -> None:
    forged = BetfairReadCompletenessWitness(
        operation="listCurrentOrders",
        completeness=BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
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



def test_reconstructed_empty_result_cannot_inherit_genuine_empty_authority() -> None:
    observer = _observer(_rpc({"currentOrders": [], "moreAvailable": False}, 1))
    genuine = observer.read_current_orders(page_size=10)

    rebound = BetfairPagedReadResult(genuine.items, genuine.witness)

    assert genuine.authoritative_empty is True
    assert rebound.authoritative_empty is False
    with pytest.raises(BetfairReadOnlyError, match="result was not issued"):
        rebound.assert_complete()


def test_paged_result_rejects_content_count_rebinding_before_authority_check() -> None:
    nonempty = _observer(
        _rpc({"currentOrders": [_current_order("bet-1")], "moreAvailable": False}, 1)
    ).read_current_orders(page_size=10)

    with pytest.raises(BetfairReadOnlyError, match="rows do not match"):
        BetfairPagedReadResult((), nonempty.witness)


def test_same_count_different_row_cannot_inherit_genuine_result_authority() -> None:
    left = _observer(
        _rpc({"currentOrders": [_current_order("bet-left")], "moreAvailable": False}, 1)
    ).read_current_orders(page_size=10)
    right = _observer(
        _rpc({"currentOrders": [_current_order("bet-right")], "moreAvailable": False}, 1)
    ).read_current_orders(page_size=10)

    rebound = BetfairPagedReadResult(right.items, left.witness)

    with pytest.raises(BetfairReadOnlyError, match="result was not issued"):
        rebound.assert_complete()
    assert left.assert_complete()[0].bet_id == "bet-left"


def test_account_funds_value_cannot_be_rebound_under_genuine_witness() -> None:
    first = _observer(
        _rpc(
            {
                "availableToBetBalance": 100,
                "exposure": -5,
                "retainedCommission": 0,
                "exposureLimit": -1000,
            },
            1,
        )
    ).read_account_funds()
    second = _observer(
        _rpc(
            {
                "availableToBetBalance": 999,
                "exposure": -1,
                "retainedCommission": 0,
                "exposureLimit": -2000,
            },
            1,
        )
    ).read_account_funds()

    rebound = BetfairValueReadResult(second.value, first.witness)

    with pytest.raises(BetfairReadOnlyError, match="result was not issued"):
        rebound.assert_complete()
    assert str(first.assert_complete().available_to_bet_balance) == "100"

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
