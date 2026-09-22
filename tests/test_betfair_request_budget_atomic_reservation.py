from __future__ import annotations

from autosport.betfair_request_budget import (
    BetfairAdmissionDecision,
    BetfairRequestBudgetPolicy,
    BetfairRequestBudgetState,
    BetfairRequestIntent,
    BetfairRequestOperation,
    BetfairRequestPriority,
    admit_betfair_request,
)


def _policy() -> BetfairRequestBudgetPolicy:
    return BetfairRequestBudgetPolicy(
        shared_order_read_pending_limit=2,
        shared_order_read_reconciliation_reserve=1,
        cleared_orders_pending_limit=2,
        market_data_pending_limit=1,
        mutation_pending_limit=1,
        read_backoff_base_ms=100,
        read_backoff_max_ms=800,
    )


def _market(request_id: str, market_id: str) -> BetfairRequestIntent:
    return BetfairRequestIntent(
        request_id=request_id,
        operation=BetfairRequestOperation.LIST_MARKET_BOOK,
        priority=BetfairRequestPriority.EXECUTION_READ,
        market_ids=(market_id,),
        market_data_weight_per_market=1,
    )


def _ordinary_current_orders(request_id: str) -> BetfairRequestIntent:
    return BetfairRequestIntent(
        request_id=request_id,
        operation=BetfairRequestOperation.LIST_CURRENT_ORDERS,
        priority=BetfairRequestPriority.EXECUTION_READ,
    )


def test_stale_state_cannot_double_admit_last_market_data_slot() -> None:
    """Admission must reserve capacity atomically, not only inspect a stale snapshot."""

    state = BetfairRequestBudgetState(in_flight_market_data=0)
    policy = _policy()

    first = admit_betfair_request(
        _market("market-a", "1.100"),
        state=state,
        policy=policy,
        now_monotonic_ns=1,
    )
    second = admit_betfair_request(
        _market("market-b", "1.200"),
        state=state,
        policy=policy,
        now_monotonic_ns=1,
    )

    admitted = sum(
        decision.decision is BetfairAdmissionDecision.ADMIT
        for decision in (first, second)
    )
    assert admitted == 1, (
        "two dispatchers that observe the same last-capacity state must not both "
        "receive ADMIT before either has reserved the provider slot"
    )


def test_stale_state_cannot_double_admit_reserved_shared_order_capacity() -> None:
    """The reconciliation reserve cannot be protected by a check-then-act race."""

    state = BetfairRequestBudgetState(in_flight_shared_order_reads=0)
    policy = _policy()

    first = admit_betfair_request(
        _ordinary_current_orders("orders-a"),
        state=state,
        policy=policy,
        now_monotonic_ns=1,
    )
    second = admit_betfair_request(
        _ordinary_current_orders("orders-b"),
        state=state,
        policy=policy,
        now_monotonic_ns=1,
    )

    admitted = sum(
        decision.decision is BetfairAdmissionDecision.ADMIT
        for decision in (first, second)
    )
    assert admitted == 1, (
        "ordinary shared-order reads must not both consume the single non-reserved "
        "slot when they race from the same immutable state"
    )
