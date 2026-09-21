from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from autosport.betfair_account_readonly import BetfairReadOnlyError
from autosport.betfair_request_budget import (
    GET_ACCOUNT_DETAILS,
    LIST_CLEARED_ORDERS,
    LIST_CURRENT_ORDERS,
    LIST_MARKET_BOOK,
    LIST_MARKET_PROFIT_AND_LOSS,
    BetfairRequestBudget,
    BetfairRequestBudgetError,
    BetfairRequestIntent,
    BetfairRequestPool,
    BetfairRequestPriority,
    BudgetedBetfairReadTransport,
)


ENDPOINT = "https://api.betfair.com/exchange/betting/json-rpc/v1"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int | float) -> None:
        self.value += timedelta(**kwargs)


class FakeTransport:
    def __init__(
        self,
        payload: bytes | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload or b'{"jsonrpc":"2.0","result":[],"id":1}'
        self.error = error
        self.calls: list[tuple[str, bytes]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append((url, body))
        if self.error is not None:
            raise self.error
        return self.payload


def _clock() -> MutableClock:
    return MutableClock(datetime(2026, 9, 21, 9, 30, tzinfo=timezone.utc))


def _body(
    method: str,
    params: dict[str, object] | None = None,
    request_id: int = 1,
) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": request_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _budget(tmp_path, clock: MutableClock, **kwargs) -> BetfairRequestBudget:
    return BetfairRequestBudget(
        tmp_path / "betfair-request-budget.json",
        clock=clock,
        **kwargs,
    )


def test_reserved_shared_capacity_keeps_reconciliation_admissible(tmp_path) -> None:
    clock = _clock()
    budget = _budget(
        tmp_path,
        clock,
        shared_capacity=3,
        reserved_reconciliation_slots=1,
    )
    background = BetfairRequestIntent(
        method=LIST_MARKET_PROFIT_AND_LOSS,
        priority=BetfairRequestPriority.EXECUTION_READ,
        pool=BetfairRequestPool.SHARED_ORDER_READS,
    )
    reconciliation = BetfairRequestIntent(
        method=LIST_CURRENT_ORDERS,
        priority=BetfairRequestPriority.RECONCILIATION,
        pool=BetfairRequestPool.SHARED_ORDER_READS,
    )

    first = budget.acquire(background)
    second = budget.acquire(background)
    try:
        with pytest.raises(BetfairRequestBudgetError, match="concurrency budget"):
            budget.acquire(background)
        urgent = budget.acquire(reconciliation)
        urgent.close()
    finally:
        first.close()
        second.close()


def test_caller_cannot_self_promote_background_method_to_reconciliation(tmp_path) -> None:
    budget = _budget(tmp_path, _clock())
    forged = BetfairRequestIntent(
        method=LIST_MARKET_PROFIT_AND_LOSS,
        priority=BetfairRequestPriority.RECONCILIATION,
        pool=BetfairRequestPool.SHARED_ORDER_READS,
    )
    with pytest.raises(BetfairRequestBudgetError, match="priority"):
        budget.acquire(forged)


def test_list_cleared_orders_uses_separate_pool_during_shared_backpressure(tmp_path) -> None:
    clock = _clock()
    budget = _budget(tmp_path, clock)
    budget.record_backpressure(BetfairRequestPool.SHARED_ORDER_READS)

    shared = BetfairRequestIntent(
        method=LIST_CURRENT_ORDERS,
        priority=BetfairRequestPriority.RECONCILIATION,
        pool=BetfairRequestPool.SHARED_ORDER_READS,
    )
    cleared = BetfairRequestIntent(
        method=LIST_CLEARED_ORDERS,
        priority=BetfairRequestPriority.RECONCILIATION,
        pool=BetfairRequestPool.CLEARED_ORDERS,
    )
    with pytest.raises(BetfairRequestBudgetError, match="cooldown"):
        budget.acquire(shared)
    lease = budget.acquire(cleared)
    lease.close()


def test_sixth_same_market_dispatch_inside_one_second_is_rejected_across_restart(
    tmp_path,
) -> None:
    clock = _clock()
    state_path = tmp_path / "betfair-request-budget.json"
    intent = BetfairRequestIntent(
        method=LIST_MARKET_BOOK,
        priority=BetfairRequestPriority.MONITORING,
        pool=BetfairRequestPool.MARKET_DATA,
        market_ids=("1.2345",),
        request_weight_points=20,
    )
    budget = BetfairRequestBudget(state_path, clock=clock)
    for _ in range(5):
        lease = budget.acquire(intent)
        lease.close()

    reopened = BetfairRequestBudget(state_path, clock=clock)
    with pytest.raises(BetfairRequestBudgetError, match="rate limit"):
        reopened.acquire(intent)

    clock.advance(seconds=1, microseconds=1)
    lease = reopened.acquire(intent)
    lease.close()


def test_list_market_book_weight_above_provider_ceiling_fails_before_dispatch(
    tmp_path,
) -> None:
    clock = _clock()
    budget = _budget(tmp_path, clock)
    transport = FakeTransport()
    wrapped = BudgetedBetfairReadTransport(
        transport,
        budget,
        market_data_weight_resolver=lambda params: 201,
    )
    with pytest.raises(BetfairRequestBudgetError, match="1..200"):
        wrapped.post(
            ENDPOINT,
            headers={},
            body=_body(LIST_MARKET_BOOK, {"marketIds": ["1.1"]}),
            timeout_seconds=1,
        )
    assert transport.calls == []


def test_list_market_book_requires_explicit_weight_resolver(tmp_path) -> None:
    transport = FakeTransport()
    wrapped = BudgetedBetfairReadTransport(
        transport,
        _budget(tmp_path, _clock()),
    )
    with pytest.raises(BetfairRequestBudgetError, match="weight resolver"):
        wrapped.post(
            ENDPOINT,
            headers={},
            body=_body(LIST_MARKET_BOOK, {"marketIds": ["1.1"]}),
            timeout_seconds=1,
        )
    assert transport.calls == []


def test_provider_backpressure_is_durable_and_never_auto_retried(tmp_path) -> None:
    clock = _clock()
    state_path = tmp_path / "betfair-request-budget.json"
    pressure = FakeTransport(
        b'{"jsonrpc":"2.0","error":{"code":-32099,"message":"ANGX-0003","data":{"APINGException":{"requestUUID":"r-1","errorCode":"TOO_MANY_REQUESTS"}}},"id":1}'
    )
    first = BudgetedBetfairReadTransport(
        pressure,
        BetfairRequestBudget(state_path, clock=clock),
    )

    payload = first.post(
        ENDPOINT,
        headers={"X-Authentication": "secret-not-persisted"},
        body=_body(LIST_CURRENT_ORDERS),
        timeout_seconds=1,
    )
    assert b"TOO_MANY_REQUESTS" in payload
    assert len(pressure.calls) == 1

    fresh_transport = FakeTransport()
    reopened = BudgetedBetfairReadTransport(
        fresh_transport,
        BetfairRequestBudget(state_path, clock=clock),
    )
    with pytest.raises(BetfairRequestBudgetError, match="cooldown"):
        reopened.post(
            ENDPOINT,
            headers={},
            body=_body(LIST_CURRENT_ORDERS),
            timeout_seconds=1,
        )
    assert fresh_transport.calls == []

    durable = state_path.read_text(encoding="utf-8")
    assert "secret-not-persisted" not in durable
    assert "TOO_MANY_REQUESTS" not in durable

    clock.advance(milliseconds=251)
    reopened.post(
        ENDPOINT,
        headers={},
        body=_body(LIST_CURRENT_ORDERS, request_id=2),
        timeout_seconds=1,
    )
    assert len(fresh_transport.calls) == 1


def test_http_429_records_backpressure_without_retry(tmp_path) -> None:
    clock = _clock()
    state_path = tmp_path / "betfair-request-budget.json"
    failing = FakeTransport(
        error=BetfairReadOnlyError("Betfair HTTP request failed with status 429")
    )
    wrapped = BudgetedBetfairReadTransport(
        failing,
        BetfairRequestBudget(state_path, clock=clock),
    )
    with pytest.raises(BetfairReadOnlyError, match="429"):
        wrapped.post(
            ENDPOINT,
            headers={},
            body=_body(LIST_CURRENT_ORDERS),
            timeout_seconds=1,
        )
    assert len(failing.calls) == 1

    reopened = BetfairRequestBudget(state_path, clock=clock)
    intent = BetfairRequestIntent(
        method=LIST_CURRENT_ORDERS,
        priority=BetfairRequestPriority.RECONCILIATION,
        pool=BetfairRequestPool.SHARED_ORDER_READS,
    )
    with pytest.raises(BetfairRequestBudgetError, match="cooldown"):
        reopened.acquire(intent)


def test_active_backpressure_is_not_erased_by_concurrent_success(tmp_path) -> None:
    clock = _clock()
    budget = _budget(tmp_path, clock)
    budget.record_backpressure(BetfairRequestPool.SHARED_ORDER_READS)
    budget.record_success(BetfairRequestPool.SHARED_ORDER_READS)

    intent = BetfairRequestIntent(
        method=LIST_CURRENT_ORDERS,
        priority=BetfairRequestPriority.RECONCILIATION,
        pool=BetfairRequestPool.SHARED_ORDER_READS,
    )
    with pytest.raises(BetfairRequestBudgetError, match="cooldown"):
        budget.acquire(intent)


def test_duplicate_request_json_key_is_rejected_before_network(tmp_path) -> None:
    transport = FakeTransport()
    wrapped = BudgetedBetfairReadTransport(
        transport,
        _budget(tmp_path, _clock()),
    )
    body = (
        b'{"jsonrpc":"2.0","method":"SportsAPING/v1.0/listCurrentOrders",'
        b'"method":"AccountAPING/v1.0/getAccountDetails","params":{},"id":1}'
    )
    with pytest.raises(BetfairRequestBudgetError, match="duplicate JSON object key"):
        wrapped.post(
            ENDPOINT,
            headers={},
            body=body,
            timeout_seconds=1,
        )
    assert transport.calls == []


def test_provider_write_method_is_outside_budgeted_read_transport(tmp_path) -> None:
    transport = FakeTransport()
    wrapped = BudgetedBetfairReadTransport(
        transport,
        _budget(tmp_path, _clock()),
    )
    with pytest.raises(BetfairRequestBudgetError, match="refuses provider mutation"):
        wrapped.post(
            ENDPOINT,
            headers={},
            body=_body("SportsAPING/v1.0/placeOrders", {"marketId": "1.1"}),
            timeout_seconds=1,
        )
    assert transport.calls == []


def test_tampered_durable_state_fails_closed_on_restart(tmp_path) -> None:
    clock = _clock()
    state_path = tmp_path / "betfair-request-budget.json"
    budget = BetfairRequestBudget(state_path, clock=clock)
    lease = budget.acquire(
        BetfairRequestIntent(
            method=GET_ACCOUNT_DETAILS,
            priority=BetfairRequestPriority.BACKGROUND,
            pool=BetfairRequestPool.OTHER_READS,
        )
    )
    lease.close()

    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["last_seen_at"] = "2030-01-01T00:00:00Z"
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(BetfairRequestBudgetError, match="digest mismatch"):
        BetfairRequestBudget(state_path, clock=clock)


def test_clock_rollback_across_restart_fails_closed(tmp_path) -> None:
    clock = _clock()
    state_path = tmp_path / "betfair-request-budget.json"
    budget = BetfairRequestBudget(state_path, clock=clock)
    intent = BetfairRequestIntent(
        method=GET_ACCOUNT_DETAILS,
        priority=BetfairRequestPriority.BACKGROUND,
        pool=BetfairRequestPool.OTHER_READS,
    )
    lease = budget.acquire(intent)
    lease.close()

    clock.value -= timedelta(seconds=1)
    reopened = BetfairRequestBudget(state_path, clock=clock)
    with pytest.raises(BetfairRequestBudgetError, match="clock moved backwards"):
        reopened.acquire(intent)
